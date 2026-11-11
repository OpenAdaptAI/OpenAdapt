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

from openadapt_flow.runtime.effects._common import (
    drop_record_fields,
    judge_records,
    page_snapshot_token,
)
from openadapt_flow.runtime.effects.adapter import VerifierAdapterBase
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
    Verdict,
    stable_id,
)
from openadapt_flow.verification import VerificationTier

#: A paged read longer than this many pages reads as unreadable.
MAX_PAGES = 50

_UNREADABLE = object()
_MISSING = object()


def _dotted(body: Any, path: str) -> Any:
    node = body
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _dotted_value(body: Any, path: str) -> Any:
    """Return a distinct sentinel when a configured path is absent."""

    node = body
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
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
        records_path: Path whose GET returns the records document. Required:
            every app keeps its records somewhere else.
        records_key: Key in the JSON body holding the records list; when the
            body is itself a JSON list, pass ``None``. Required.
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
            page on the same origin and endpoint path is read (at most :data:`MAX_PAGES`); a
            failed page or a longer chain reads as unreadable. Unset, one
            response is not assumed to be the whole read.
        total_path: Dotted path in every page's body to the number of
            records the server holds for the read. Set, a read whose record
            count differs, or a body without it, reads as unreadable. Unset,
            the read does not gain a completeness guarantee from the total.
        records_complete: True only when a learned endpoint contract declares
            that one response contains the complete matching record set. This
            is for validated, unpaged endpoints; it is never inferred from
            response length.
        list_keys: The top-level keys whose value is a list in a whole-store
            read that holds one list per collection, as the read was reviewed.
            Set, a body that is not a JSON object, lacks a list under one of
            these keys, or holds an object value (which may contain records no
            reviewed read sees), reads as unreadable.
        other_lists: With ``list_keys`` (and ``records_key=None``): the read
            returns the records of every list under a key NOT in
            ``list_keys``. A collection that appears after review is read
            here, so a row written to it is seen instead of passing unread.
        snapshot_path: Dotted body path to the immutable view identifier in a
            reviewed endpoint contract. Multi-page reads require the same
            exact token on every page; its observed stability alone does not
            establish the endpoint's snapshot semantics.

    Why the last read was unreadable is in :attr:`read_problem` (and in the
    state's ``detail`` and the verdict's reason).
    """

    substrate = "rest"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(
        self,
        base_url: str,
        *,
        records_path: str,
        records_key: Optional[str],
        session: Any = None,
        headers: Optional[dict[str, str]] = None,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.2,
        drop_fields: tuple[str, ...] = (),
        next_path: Optional[str] = None,
        total_path: Optional[str] = None,
        records_complete: Optional[bool] = None,
        snapshot_path: Optional[str] = None,
        consistency: Optional[str] = None,
        quiescence: Any = None,
        list_keys: Optional[tuple[str, ...]] = None,
        other_lists: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.list_keys = None if list_keys is None else frozenset(list_keys)
        self.other_lists = bool(other_lists)
        if self.other_lists and (self.list_keys is None or records_key is not None):
            raise ValueError("other_lists needs list_keys and no records_key")
        self.next_path = next_path
        self.total_path = total_path
        self.records_complete = records_complete
        self.snapshot_path = snapshot_path
        self.consistency = consistency
        self.quiescence = quiescence
        if consistency not in (None, "quiescent-two-pass") or (
            consistency and snapshot_path
        ):
            raise ValueError("unsupported or conflicting REST consistency contract")
        self.readback_complete: Optional[bool] = None
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
        if resp.status_code == 206:
            self.read_problem = f"GET {url} returned HTTP 206 Partial Content"
            self.readback_complete = False
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
        if self.other_lists:
            if not isinstance(body, dict):
                return None
            return [
                item
                for key in sorted(body, key=str)
                if key not in (self.list_keys or ()) and isinstance(body[key], list)
                for item in body[key]
            ]
        if self.records_key is None:
            records = body
        elif isinstance(body, dict):
            records = body.get(self.records_key)
        else:
            return None
        return records if isinstance(records, list) else None

    def _fetch_records(self) -> Optional[list[dict[str, Any]]]:
        if self.consistency is None:
            return self._fetch_pass()
        from openadapt_flow.runtime.effects.quiescence import two_pass_read

        self.readback_complete = False
        try:
            if not self.total_path or not self.next_path:
                raise ValueError(
                    "quiescent REST reads require total and terminal next semantics"
                )
            records = two_pass_read(
                self.quiescence,
                {
                    "kind": "rest",
                    "base_url": self.base_url,
                    "records_path": self.records_path,
                    "records_key": self.records_key,
                    "total_path": self.total_path,
                    "next_path": self.next_path,
                },
                self._fetch_pass,
            )
        except Exception:  # noqa: BLE001 - failed exclusion/coverage cannot confirm
            self.read_problem = "quiescent complete-read validation failed"
            self.readback_complete = False
            return None
        self.readback_complete = True
        return drop_record_fields(records, self.drop_fields)

    def _fetch_pass(
        self, check_quiescence: Any = None
    ) -> Optional[list[dict[str, Any]]]:
        """GET the system-of-record document and extract the records list.

        Returns ``None`` -- read as unreadable, forcing INDETERMINATE -- on
        any transport error, non-2xx status, or shape mismatch, and, with
        ``next_path``, on a failed page, a page on another origin or a chain
        longer than :data:`MAX_PAGES`. Never raises.
        """
        self.read_problem = None
        self.readback_complete = None
        if check_quiescence is not None:
            reset_pass = getattr(self._get_session(), "begin_read_pass", None)
            if callable(reset_pass):
                reset_pass()
        url = f"{self.base_url}{self.records_path}"
        endpoint_path = urlsplit(url).path
        collected: list[Any] = []
        total: Any = None
        next_chain_complete = True
        snapshot: str | int | object = _MISSING
        for page in range(MAX_PAGES):
            if check_quiescence is not None:
                check_quiescence()
            body = self._get_json(url)
            if check_quiescence is not None:
                check_quiescence()
            if body is _UNREADABLE:
                self.readback_complete = False
                return None
            if self.snapshot_path is not None:
                try:
                    current_snapshot = page_snapshot_token(body, self.snapshot_path)
                except ValueError as error:
                    self.read_problem = str(error)
                    self.readback_complete = False
                    return None
                if snapshot is not _MISSING and (
                    type(current_snapshot) is not type(snapshot)
                    or current_snapshot != snapshot
                ):
                    self.read_problem = "the declared snapshot changed across pages"
                    self.readback_complete = False
                    return None
                snapshot = current_snapshot
            if self.list_keys is not None:
                missing = (
                    sorted(
                        k for k in self.list_keys if not isinstance(body.get(k), list)
                    )
                    if isinstance(body, dict)
                    else ["(the body is not an object)"]
                )
                if missing:
                    self.read_problem = (
                        f"GET {url}: the reviewed lists are missing: "
                        + ", ".join(missing)
                    )
                    self.readback_complete = False
                    return None
                nested = sorted(str(k) for k, v in body.items() if isinstance(v, dict))
                if nested:
                    # An object may hold records no reviewed read sees.
                    self.read_problem = (
                        f"GET {url}: the body holds an object that may contain "
                        "records: " + ", ".join(nested)
                    )
                    self.readback_complete = False
                    return None
            records = self._records_of(body)
            if records is None:
                self.read_problem = f"GET {url}: no records list in the body"
                self.readback_complete = False
                return None
            if self.total_path is not None:
                current_total = _dotted(body, self.total_path)
                if (
                    isinstance(current_total, bool)
                    or not isinstance(current_total, int)
                    or current_total < 0
                ):
                    self.read_problem = f"no record total at {self.total_path}"
                    self.readback_complete = False
                    return None
                if page and current_total != total:
                    self.read_problem = "the declared record total changed across pages"
                    self.readback_complete = False
                    return None
                total = current_total
            collected.extend(records)
            if self.next_path is None:
                break
            following = _dotted_value(body, self.next_path)
            if following is _MISSING:
                next_chain_complete = False
                break
            if following in (None, ""):
                break
            if not isinstance(following, str):
                self.read_problem = f"the next link at {self.next_path} is not a URL"
                self.readback_complete = False
                return None
            following = urljoin(url, following)
            if not _same_origin(following, self.base_url):
                # OpenEMR, for one, builds next links from its configured site
                # address, which can differ from the address the read uses.
                self.read_problem = (
                    f"next link origin {_origin(following)} != base_url "
                    f"{_origin(self.base_url)}"
                )
                self.readback_complete = False
                return None
            # Query cursors may vary; a next link cannot extend the declared
            # read to another endpoint or smuggle a fragment-only page.
            if (
                urlsplit(following).path != endpoint_path
                or urlsplit(following).fragment
            ):
                self.read_problem = "next link leaves the declared endpoint path"
                self.readback_complete = False
                return None
            if self.snapshot_path is None and check_quiescence is None:
                self.read_problem = (
                    "multi-page reads require declared immutable snapshot semantics"
                )
                self.readback_complete = False
                return None
            url = following
        else:
            self.read_problem = f"more than {MAX_PAGES} pages"
            self.readback_complete = False
            return None
        if self.total_path is not None:
            if isinstance(total, bool) or not isinstance(total, int):
                self.read_problem = f"no record total at {self.total_path}"
                self.readback_complete = False
                return None
            if total != len(collected):
                self.read_problem = (
                    f"the record total at {self.total_path} is {total}, the read "
                    f"returned {len(collected)}"
                )
                self.readback_complete = False
                return None
        if any(not isinstance(record, dict) for record in collected):
            self.read_problem = f"GET {url}: records list contains a non-object"
            self.readback_complete = False
            return None
        identities: set[Any] = set()
        identity_domain: type | None = None
        for record in collected:
            if "id" not in record and not (page == 0 and self.records_complete is True):
                self.read_problem = (
                    "paged or counted reads require a usable record identity"
                )
                self.readback_complete = False
                return None
            if "id" in record and (
                not isinstance(record["id"], (str, int))
                or isinstance(record["id"], bool)
                or (isinstance(record["id"], str) and not record["id"].strip())
            ):
                self.read_problem = "REST read has no usable record identity"
                self.readback_complete = False
                return None
            if "id" in record:
                domain = type(record["id"])
                if identity_domain is not None and domain is not identity_domain:
                    self.read_problem = (
                        "REST read mixes record identity domains; coverage is ambiguous"
                    )
                    self.readback_complete = False
                    return None
                identity_domain = domain
            try:
                identity = stable_id(record)
                repeated = identity in identities
            except TypeError:
                self.read_problem = "REST read has no usable record identity"
                self.readback_complete = False
                return None
            if repeated:
                # A repeated representation is not another covered record.
                # Silently removing it could still hide an omitted record.
                self.read_problem = (
                    "REST read repeated a record identity; coverage is ambiguous"
                )
                self.readback_complete = False
                return None
            identities.add(identity)
        if self.total_path is not None:
            self.readback_complete = True
        elif self.next_path is not None:
            self.readback_complete = next_chain_complete
        elif self.records_complete is True:
            self.readback_complete = True
        elif self.records_complete is False:
            self.readback_complete = False
        if self.readback_complete is not True:
            self.read_problem = "REST read completeness was not established"
            return None
        return (
            collected
            if check_quiescence is not None
            else drop_record_fields(collected, self.drop_fields)
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
            readback_complete=self.readback_complete,
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
            current_complete = self.readback_complete
            if current_complete is not True or (
                before.readback_complete is not True
                and last.verdict is not Verdict.REFUTED
            ):
                read_problem = (
                    "baseline" if before.readback_complete is not True else "current"
                )
                last = last.model_copy(
                    update={
                        "verdict": Verdict.INDETERMINATE,
                        "reason": (
                            f"{last.reason} (REST {read_problem} read completeness "
                            "was not established)"
                        ),
                        "readback_complete": current_complete,
                    }
                )
            else:
                last = last.model_copy(update={"readback_complete": current_complete})
            if current is None and self.read_problem:
                last = last.model_copy(
                    update={"reason": f"{last.reason} ({self.read_problem})"}
                )
            if last.confirmed or time.monotonic() >= deadline:
                return last
            time.sleep(self.poll_interval_s)
