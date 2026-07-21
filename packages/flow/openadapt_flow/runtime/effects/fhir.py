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

LIVE vs contract-gated: point it at a real OpenEMR by setting
``OPENEMR_FHIR_BASE_URL`` (+ a bearer token); tests that need a live instance
are skipped when it is absent. CI exercises it against an in-repo fake that
emits byte-faithful FHIR R4 ``Bundle`` JSON (``tests`` fixtures) -- the fake
mirrors the real FHIR wire shape, never MockMed's screen.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
)

#: OpenEMR's FHIR field paths for the canonical patient-note-as-Observation
#: write. Callers override for DocumentReference / Encounter / etc.
DEFAULT_OBSERVATION_PATHS: dict[str, str] = {
    "id": "id",
    "patient": "subject.reference",
    "status": "status",
    "note": "valueString",
    "code": "code.text",
}


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


class FhirEffectVerifier:
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
        session: Optional ``requests``-style session (tests / custom auth).
        timeout_s: Per-request timeout.
        verify_tls: Passed through to the HTTP client's ``verify``.
        poll_interval_s: Gap between polls while waiting for the write.
    """

    substrate = "fhir"

    def __init__(
        self,
        base_url: str,
        *,
        resource_type: str = "Observation",
        search_params: Optional[dict[str, str]] = None,
        field_paths: Optional[dict[str, str]] = None,
        access_token: Optional[str] = None,
        session: Any = None,
        timeout_s: float = 5.0,
        verify_tls: bool = True,
        poll_interval_s: float = 0.3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.resource_type = resource_type
        self.search_params = dict(search_params or {})
        self.field_paths = dict(field_paths or DEFAULT_OBSERVATION_PATHS)
        self.access_token = access_token
        self.timeout_s = timeout_s
        self.verify_tls = verify_tls
        self.poll_interval_s = poll_interval_s
        self._session = session

    # -- transport ----------------------------------------------------------

    def _get_session(self) -> Any:
        if self._session is None:
            import requests  # lazy

            self._session = requests.Session()
        return self._session

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/fhir+json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
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
        try:
            resp = self._get_session().get(
                url,
                params=self.search_params,
                headers=self._headers(),
                timeout=self.timeout_s,
                verify=self.verify_tls,
            )
        except Exception:  # noqa: BLE001 - transport failure is unreadable
            return None
        if resp.status_code // 100 != 2:
            return None
        try:
            bundle = resp.json()
        except Exception:  # noqa: BLE001 - unparseable body is unreadable
            return None
        if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
            return None
        entries = bundle.get("entry", [])
        if not isinstance(entries, list):
            return None
        out: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                return None
            resource = entry.get("resource")
            if not isinstance(resource, dict):
                return None
            out.append(self._flatten(resource))
        return out

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
