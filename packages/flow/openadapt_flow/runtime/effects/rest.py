"""REST system-of-record :class:`EffectVerifier`.

Verifies a typed :class:`Effect` against a REST/JSON system of record: an
HTTP endpoint that returns the authoritative records (an EMR's own JSON API,
an internal service, the MockMed transactional back end at ``GET /api/db``).
This is the ``api`` implementation tier of the RFC's transition contract
(``docs/design/WORKFLOW_PROGRAM_IR.md`` section 4: "call the app's API / DB
write; effect probed against the system of record") for a plain-JSON back end.

It reads the system of record over the network -- NOT the screen -- so it
catches every transactional fault a vision postcondition is blind to: a
partial save (a persisted row missing a field), a phantom / optimistic-UI
success (nothing landed), a duplicate or double-delivered write (two rows),
and a stale last-write-wins overwrite (a concurrent row destroyed).

Fail-safe: any transport error, non-2xx status, or unparseable body makes the
verifier read the system of record as *unreadable* -> INDETERMINATE -> HALT,
never a guessed success.
"""

from __future__ import annotations

import time
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

from openadapt_flow.runtime.effects._common import drop_record_fields, judge_records
from openadapt_flow.runtime.effects.adapter import VerifierAdapterBase
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
)
from openadapt_flow.verification import VerificationTier

#: A paged read longer than this many pages reads as unreadable.
MAX_PAGES = 50

_UNREADABLE = object()


def _dotted(body: Any, path: str) -> Any:
    node = body
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _same_origin(url: str, base_url: str) -> bool:
    return _origin(url) == _origin(base_url)


def _no_redirects(session: Any) -> dict[str, Any]:
    """Keyword that stops ``requests`` following a redirect.

    A redirect could carry the configured auth headers to another host, so a
    3xx is a failed read. ``httpx`` doesn't follow redirects by default; an
    injected session keeps its own ``get`` signature.
    """

    module = type(session).__module__ or ""
    return {"allow_redirects": False} if module.startswith("requests") else {}


class RestRecordVerifier(VerifierAdapterBase):
    """Verify effects against a JSON REST system of record.

    Args:
        base_url: Base URL of the system of record (trailing slash optional).
        records_path: Path whose GET returns the records document.
        records_key: Key in the JSON body holding the records list; when the
            body is itself a JSON list, pass ``None``.
        session: Optional ``requests``-style session (injectable for tests /
            auth headers); a module-level default is used when omitted.
        headers: Optional headers sent with every read (the kit's
            secret-isolated auth path: ``effects.auth`` in a deployment YAML
            resolves an env-var reference to these headers at construction --
            see ``runtime.effects.auth.AuthRef``). ``None`` -> no extra
            headers, byte-identical to the pre-kit behavior.
        timeout_s: Per-request timeout in seconds.
        poll_interval_s: Gap between reachability retries while polling for
            the write to land within ``Effect.timeout_s``.
        drop_fields: Record fields removed before judging (``["id"]`` judges
            records by their whole content, so a change to another record
            reads as that record removed and a new one added, and the
            collateral-loss check catches it). Empty -> records as read.
        next_path: Dotted path in the body to the next page's URL. Set, every
            page on the same origin is read (at most :data:`MAX_PAGES`); a
            failed page or a longer chain reads as unreadable. Unset, one
            response is the whole read.
        total_path: Dotted path in the first page's body to the number of
            records the server holds for the read. Set, a read whose record
            count differs, or a body without it, reads as unreadable. Unset,
            the read trusts the page chain.

    Why the last read was unreadable is in :attr:`read_problem` (and in the
    state's ``detail`` and the verdict's reason).
    """

    substrate = "rest"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(
        self,
        base_url: str,
        *,
        records_path: str = "/api/db",
        records_key: Optional[str] = "records",
        session: Any = None,
        headers: Optional[dict[str, str]] = None,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.2,
        drop_fields: tuple[str, ...] = (),
        next_path: Optional[str] = None,
        total_path: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.next_path = next_path
        self.total_path = total_path
        self.read_problem: Optional[str] = None
        self.drop_fields = tuple(drop_fields)
        self.records_path = records_path
        self.records_key = records_key
        self.headers = dict(headers) if headers else None
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self._session = session

    # -- transport ----------------------------------------------------------

    def _get_session(self) -> Any:
        """Return the read client, preferring an injected one.

        ``requests`` is used when present (unchanged for every deployment that
        already has it) and ``httpx`` otherwise.  ``httpx`` is a core
        dependency while ``requests`` is not, so without this fallback a clean
        install could not read a REST system of record at all: every verify
        raised inside :meth:`_fetch_records`, was read as *unreadable*, and
        HALTed.  That failed safe -- it never invented a confirmation -- but it
        made ``effects.kind: rest`` unusable out of the box.  Both clients
        expose the same ``get(url, timeout=..., headers=...)`` surface and the
        same ``status_code`` / ``json()`` on the response.
        """
        if self._session is None:
            try:
                import requests  # lazy: keep module import light
            except ModuleNotFoundError:
                import httpx

                self._session = httpx.Client()
            else:
                self._session = requests.Session()
        return self._session

    def _get_json(self, url: str) -> Any:
        """One GET; the parsed body, or ``_UNREADABLE``. Never raises."""

        session = self._get_session()
        try:
            # ``headers`` is passed ONLY when configured, so injected
            # ``requests``-style sessions with a narrower ``get`` signature
            # (tests, custom oracles) keep working exactly as before.
            if self.headers:
                resp = session.get(
                    url,
                    timeout=self.timeout_s,
                    headers=self.headers,
                    **_no_redirects(session),
                )
            else:
                resp = session.get(url, timeout=self.timeout_s)
        except Exception as exc:  # noqa: BLE001 - any transport failure is unreadable
            self.read_problem = f"GET {url} failed: {type(exc).__name__}"
            return _UNREADABLE
        if resp.status_code // 100 != 2:
            self.read_problem = f"GET {url} returned HTTP {resp.status_code}"
            return _UNREADABLE
        landed = getattr(resp, "url", None)
        if isinstance(landed, str) and landed and not _same_origin(landed, url):
            self.read_problem = f"GET {url} was redirected to origin {_origin(landed)}"
            return _UNREADABLE
        try:
            return resp.json()
        except Exception:  # noqa: BLE001 - unparseable body is unreadable
            self.read_problem = f"GET {url} returned a body that is not JSON"
            return _UNREADABLE

    def _records_of(self, body: Any) -> Optional[list[Any]]:
        if self.records_key is None:
            records = body
        elif isinstance(body, dict):
            records = body.get(self.records_key)
        else:
            return None
        return records if isinstance(records, list) else None

    def _fetch_records(self) -> Optional[list[dict[str, Any]]]:
        """GET the system-of-record document and extract the records list.

        Returns ``None`` -- read as unreadable, forcing INDETERMINATE -- on
        any transport error, non-2xx status, or shape mismatch, and, with
        ``next_path``, on a failed page, a page on another origin or a chain
        longer than :data:`MAX_PAGES`. Never raises.
        """
        self.read_problem = None
        url = f"{self.base_url}{self.records_path}"
        collected: list[Any] = []
        total: Any = None
        for page in range(MAX_PAGES):
            body = self._get_json(url)
            if body is _UNREADABLE:
                return None
            records = self._records_of(body)
            if records is None:
                self.read_problem = f"GET {url}: no records list in the body"
                return None
            if page == 0 and self.total_path is not None:
                total = _dotted(body, self.total_path)
            collected.extend(records)
            if self.next_path is None:
                break
            following = _dotted(body, self.next_path)
            if following in (None, ""):
                break
            if not isinstance(following, str):
                self.read_problem = f"the next link at {self.next_path} is not a URL"
                return None
            following = urljoin(url, following)
            if not _same_origin(following, self.base_url):
                # OpenEMR, for one, builds next links from its configured site
                # address, which can differ from the address the read uses.
                self.read_problem = (
                    f"next link origin {_origin(following)} != base_url "
                    f"{_origin(self.base_url)}"
                )
                return None
            url = following
        else:
            self.read_problem = f"more than {MAX_PAGES} pages"
            return None
        if self.total_path is not None:
            if isinstance(total, bool) or not isinstance(total, int):
                self.read_problem = f"no record total at {self.total_path}"
                return None
            if total != len(collected):
                self.read_problem = (
                    f"the record total at {self.total_path} is {total}, the read "
                    f"returned {len(collected)}"
                )
                return None
        return drop_record_fields(
            [r for r in collected if isinstance(r, dict)], self.drop_fields
        )

    # -- EffectVerifier protocol --------------------------------------------

    def capture_pre_state(self, context: Any = None) -> EffectState:
        records = self._fetch_records()
        detail: dict[str, Any] = {"url": f"{self.base_url}{self.records_path}"}
        if records is None and self.read_problem:
            detail["read_problem"] = self.read_problem
        return EffectState(
            substrate=self.substrate,
            reachable=records is not None,
            records=records or [],
            detail=detail,
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        # Poll for the write to land: a real back end persists slightly after
        # the GUI paints. Stop early once the effect can be judged CONFIRMED;
        # a REFUTED/absent read keeps polling until the deadline (the row may
        # still be settling) then returns the final judgement.
        deadline = time.monotonic() + max(0.0, expected.timeout_s)
        last: Optional[EffectVerdict] = None
        while True:
            current = self._fetch_records()
            last = judge_records(expected, before, current, substrate=self.substrate)
            if current is None and self.read_problem:
                last = last.model_copy(
                    update={"reason": f"{last.reason} ({self.read_problem})"}
                )
            if last.confirmed or time.monotonic() >= deadline:
                return last
            time.sleep(self.poll_interval_s)
