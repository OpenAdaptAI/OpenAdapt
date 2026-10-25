"""OpenEMR FHIR R4 system-of-record :class:`EffectVerifier` (primary substrate).

After a GUI write in OpenEMR (add a patient note / observation / encounter),
this verifier queries the FHIR R4 API and confirms the resource ACTUALLY
EXISTS in the system of record with the expected field values -- healthcare,
the wedge. It is the ``api`` implementation tier of the RFC transition
contract (``docs/design/WORKFLOW_PROGRAM_IR.md`` section 4) for a FHIR back
end, and a deliberately DIFFERENT verifier *type* from :mod:`.rest`: the
system of record answers in FHIR ``Bundle`` search-sets whose resources are
nested (``subject.reference``, ``valueString``, ``code.text``), so the same
:class:`Effect` contract is checked through a FHIR-shaped lens -- proving the
protocol is substrate-agnostic, not MockMed-shaped.

Real FHIR contract (OpenEMR ``/apis/default/fhir/R4``, HL7 FHIR R4):

    GET {base}/{ResourceType}?{search params}
    Authorization: Bearer <oauth2 access token>
    ->  200 {"resourceType":"Bundle","type":"searchset","total":N,
             "entry":[{"resource":{"resourceType":"Observation","id":...,
                        "status":"final","subject":{"reference":"Patient/9"},
                        "valueString":"..."}}, ...]}

Fields are extracted with a caller-supplied ``field_paths`` map (flat Effect
key -> dotted FHIRPath-lite), so :func:`judge_records` sees flat dicts and the
decision logic stays shared with every other substrate.

Fail-safe: a transport error, a non-2xx (incl. 401/403 expired token), a
non-Bundle body, or an unparseable entry all read as *unreadable* ->
INDETERMINATE -> HALT. An expired OAuth token can NEVER be mistaken for
"record absent"; it halts.

Short-lived tokens: pass ``token_source`` (for example a
:class:`~openadapt_flow.runtime.effects.smart_auth.SmartBackendTokenSource`)
instead of ``access_token``. Each read asks it for a token; no token means the
read is unreadable, and a 401 drops the cached token so the next poll gets a
new one.

LIVE vs contract-gated: point it at a real OpenEMR by setting
``OPENEMR_FHIR_BASE_URL`` (+ a bearer token); tests that need a live instance
are skipped when it is absent. CI exercises it against an in-repo fake that
emits byte-faithful FHIR R4 ``Bundle`` JSON (``tests`` fixtures) -- the fake
mirrors the real FHIR wire shape, never MockMed's screen.
"""

from __future__ import annotations

import time
from typing import Any, Optional, Protocol

from openadapt_flow.runtime.effects._common import drop_record_fields, judge_records
from openadapt_flow.runtime.effects.adapter import VerifierAdapterBase
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
)
from openadapt_flow.verification import VerificationTier

#: OpenEMR's FHIR field paths for the canonical patient-note-as-Observation
#: write. Callers override for DocumentReference / Encounter / etc.
DEFAULT_OBSERVATION_PATHS: dict[str, str] = {
    "id": "id",
    "patient": "subject.reference",
    "status": "status",
    "note": "valueString",
    "code": "code.text",
}


#: A search result longer than this many pages reads as unreadable, never as
#: the first pages.
MAX_PAGES = 50


def _next_link(bundle: dict[str, Any]) -> Optional[str]:
    """The Bundle's ``next`` page URL, if any."""

    links = bundle.get("link")
    if not isinstance(links, list):
        return None
    for link in links:
        if isinstance(link, dict) and link.get("relation") == "next":
            url = link.get("url")
            return url if isinstance(url, str) and url else None
    return None


def _same_origin(url: str, base_url: str) -> bool:
    from urllib.parse import urlsplit

    a, b = urlsplit(url), urlsplit(base_url)
    return (a.scheme, a.netloc) == (b.scheme, b.netloc)


class AccessTokenSource(Protocol):
    """Supplies bearer tokens that can expire."""

    def access_token(self) -> Optional[str]: ...

    def invalidate(self) -> None: ...


def extract_path(resource: dict[str, Any], dotted: str) -> Any:
    """Extract ``dotted`` (``a.b.c``) from a nested FHIR resource.

    A segment that lands on a list takes the list's first element (the common
    ``component[0]`` / single-coding case), so ``code.coding.code`` reads the
    first coding's code. Returns ``None`` when any segment is missing.
    """
    node: Any = resource
    for seg in dotted.split("."):
        if isinstance(node, list):
            node = node[0] if node else None
        if not isinstance(node, dict):
            return None
        node = node.get(seg)
    if isinstance(node, list):
        node = node[0] if node else None
    return node


class FhirEffectVerifier(VerifierAdapterBase):
    """Verify effects against an OpenEMR FHIR R4 system of record.

    Args:
        base_url: FHIR base URL (e.g.
            ``https://demo.openemr.io/apis/default/fhir/R4``).
        resource_type: FHIR resource type to search (``Observation``,
            ``DocumentReference``, ``Encounter``, ...).
        search_params: Query params identifying the candidate resource set
            (e.g. ``{"patient": "9", "category": "social-history"}``); used
            for both the pre-state snapshot and the post-action read.
        field_paths: Flat Effect key -> dotted FHIRPath-lite for flattening a
            resource into the dict :func:`judge_records` matches on.
        access_token: OAuth2 bearer token (``Authorization: Bearer ...``).
        token_source: Gets a token per read instead of ``access_token``
            (mutually exclusive with it).
        session: Optional ``requests``-style session (tests / custom auth).
        timeout_s: Per-request timeout.
        verify_tls: Passed through to the HTTP client's ``verify``.
        poll_interval_s: Gap between polls while waiting for the write.
        drop_fields: Record fields removed before judging (``["id"]`` judges
            records by their whole content); see :class:`RestRecordVerifier`.
    """

    substrate = "fhir"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(
        self,
        base_url: str,
        *,
        resource_type: str = "Observation",
        search_params: Optional[dict[str, str]] = None,
        field_paths: Optional[dict[str, str]] = None,
        access_token: Optional[str] = None,
        token_source: Optional[AccessTokenSource] = None,
        session: Any = None,
        timeout_s: float = 5.0,
        verify_tls: bool = True,
        poll_interval_s: float = 0.3,
        drop_fields: tuple[str, ...] = (),
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.drop_fields = tuple(drop_fields)
        self.resource_type = resource_type
        self.search_params = dict(search_params or {})
        self.field_paths = dict(field_paths or DEFAULT_OBSERVATION_PATHS)
        if access_token and token_source is not None:
            raise ValueError("give access_token or token_source, not both")
        self.access_token = access_token
        self.token_source = token_source
        self.timeout_s = timeout_s
        self.verify_tls = verify_tls
        self.poll_interval_s = poll_interval_s
        self._session = session
        # ``requests`` accepts ``verify=`` on every call; ``httpx`` binds TLS
        # verification when the CLIENT is constructed and rejects it per
        # request. True for a ``requests`` session and for any injected
        # (``requests``-style) session; flipped only for the httpx fallback
        # client built in :meth:`_get_session`.
        self._verify_per_request = True

    # -- transport ----------------------------------------------------------

    def _get_session(self) -> Any:
        """Return the read client, preferring an injected one.

        ``requests`` is used when present (unchanged for every deployment that
        already has it) and ``httpx`` otherwise. ``httpx`` is a core dependency
        while ``requests`` is only a dev/``windows`` extra, so without this
        fallback a clean wheel install could not read a FHIR system of record at
        all: every verify raised inside :meth:`_search`, was read as
        *unreadable*, and HALTed. That failed safe -- it never invented a
        confirmation -- but it made ``effects.kind: fhir`` unusable out of the
        box. Both clients expose the same ``get(url, params=..., headers=...,
        timeout=...)`` surface and the same ``status_code`` / ``json()`` on the
        response; only ``verify`` differs, which
        :attr:`_verify_per_request` accounts for.
        """
        if self._session is None:
            try:
                import requests  # lazy: keep module import light
            except ModuleNotFoundError:
                import httpx

                self._session = httpx.Client(verify=self.verify_tls)
                self._verify_per_request = False
            else:
                self._session = requests.Session()
        return self._session

    def _headers(self) -> Optional[dict[str, str]]:
        """Request headers, or None when the token source has no token."""
        headers = {"Accept": "application/fhir+json"}
        token = self.access_token
        if self.token_source is not None:
            token = self.token_source.access_token()
            if not token:
                return None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _flatten(self, resource: dict[str, Any]) -> dict[str, Any]:
        flat = {
            key: extract_path(resource, path) for key, path in self.field_paths.items()
        }
        # Always carry a stable id for delta / collateral accounting.
        if "id" not in flat or flat["id"] is None:
            flat["id"] = resource.get("id")
        return flat

    def _search(self) -> Optional[list[dict[str, Any]]]:
        """Run the configured FHIR search; return flattened resources.

        Returns ``None`` (read as unreadable -> INDETERMINATE) on any
        transport error, non-2xx (incl. auth 401/403), non-Bundle body, or
        malformed entries. Never raises.
        """
        url = f"{self.base_url}/{self.resource_type}"
        headers = self._headers()
        if headers is None:
            return None
        session = self._get_session()
        # ``verify`` is passed per request only for a ``requests``-style client;
        # the httpx fallback already bound it at construction (see
        # ``_get_session``), and passing it again would raise.
        tls_kwargs = {"verify": self.verify_tls} if self._verify_per_request else {}
        try:
            resp = session.get(
                url,
                params=self.search_params,
                headers=headers,
                timeout=self.timeout_s,
                **tls_kwargs,
            )
        except Exception:  # noqa: BLE001 - transport failure is unreadable
            return None
        if resp.status_code == 401 and self.token_source is not None:
            self.token_source.invalidate()
        if resp.status_code // 100 != 2:
            return None
        try:
            bundle = resp.json()
        except Exception:  # noqa: BLE001 - unparseable body is unreadable
            return None
        out: list[dict[str, Any]] = []
        total: Any = None
        for page in range(MAX_PAGES):
            if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
                return None
            if page == 0:
                total = bundle.get("total")
            entries = bundle.get("entry", [])
            if not isinstance(entries, list):
                return None
            for entry in entries:
                if not isinstance(entry, dict):
                    return None
                resource = entry.get("resource")
                if not isinstance(resource, dict):
                    return None
                out.append(self._flatten(resource))
            next_url = _next_link(bundle)
            if next_url is None:
                break
            if not _same_origin(next_url, self.base_url):
                return None  # a next page elsewhere is not this system of record
            try:
                resp = session.get(
                    next_url, headers=headers, timeout=self.timeout_s, **tls_kwargs
                )
            except Exception:  # noqa: BLE001 - a lost page makes the set partial
                return None
            if resp.status_code // 100 != 2:
                return None
            try:
                bundle = resp.json()
            except Exception:  # noqa: BLE001
                return None
        else:
            return None  # more pages than MAX_PAGES: never a truncated read
        if isinstance(total, int) and not isinstance(total, bool) and total > len(out):
            # The server says there are more matches than it returned.
            return None
        return drop_record_fields(out, self.drop_fields)

    # -- EffectVerifier protocol --------------------------------------------

    def capture_pre_state(self, context: Any = None) -> EffectState:
        records = self._search()
        return EffectState(
            substrate=self.substrate,
            reachable=records is not None,
            records=records or [],
            detail={
                "base_url": self.base_url,
                "resource_type": self.resource_type,
                "search_params": self.search_params,
            },
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        deadline = time.monotonic() + max(0.0, expected.timeout_s)
        last: Optional[EffectVerdict] = None
        while True:
            current = self._search()
            last = judge_records(expected, before, current, substrate=self.substrate)
            if last.confirmed or time.monotonic() >= deadline:
                return last
            time.sleep(self.poll_interval_s)
