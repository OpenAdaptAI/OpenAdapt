"""GraphQL read-back system-of-record :class:`EffectVerifier` adapter.

The GraphQL sibling of :class:`~openadapt_flow.runtime.effects.rest.RestRecordVerifier`:
one READ-ONLY query (a ``query`` operation; a ``mutation``/``subscription``
refuses to construct) POSTs to the endpoint with ``ValueExpr``-bound
variables, and the selected records list is judged by the shared judge exactly
like every other substrate -- at-most-once counting, ``count_new_only``,
idempotency keys, field read-back, collateral loss.

Entity + tenant binding: ``variables`` carry the run's governed parameters
(``{param: ...}`` resolved at construction by ``deployment.build_effect_verifier``),
so the query reads back exactly the record THIS run wrote -- never a hardcoded
demonstration entity.

Freshness: when the schema exposes a record timestamp, configure
``freshness_field`` + ``freshness_window_s`` and a CONFIRMED read whose
evidence lies outside the window is demoted to STALE
(:func:`~openadapt_flow.runtime.effects.adapter.enforce_freshness`) -- never a
silent pass on stale data.

Fail-safe: any transport error, non-2xx status, unparseable body, non-empty
GraphQL ``errors`` array (including auth errors, which GraphQL servers often
return with HTTP 200), or a records path that does not land on a list reads as
*unreadable* -> INDETERMINATE (UNAVAILABLE) -> HALT, never a guessed success.
"""

from __future__ import annotations

from typing import Any, Optional

from openadapt_flow.runtime.effects.adapter import (
    CollateralHook,
    RedactionPolicy,
    VerifierAdapterBase,
    apply_collateral_hooks,
    enforce_freshness,
    poll_until_settled,
    redact_verdict,
)
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
)
from openadapt_flow.verification import VerificationTier


def _root_definition_names(document: str) -> list[str]:
    """Return GraphQL definition-leading names, ignoring comments/strings.

    A document may contain several operations and fragments, so checking only
    its first word is insufficient: ``query { ... } mutation { ... }`` must
    still refuse. Names inside variable declarations or selection sets are not
    definition leaders and therefore do not create false positives.
    """
    names: list[str] = []
    index = 0
    depth = 0
    at_definition_start = True
    length = len(document)
    while index < length:
        char = document[index]
        if char in " \t\r\n,":
            index += 1
            continue
        if char == "#":
            newline = document.find("\n", index + 1)
            index = length if newline < 0 else newline + 1
            continue
        if document.startswith('"""', index):
            end = document.find('"""', index + 3)
            index = length if end < 0 else end + 3
            continue
        if char == '"':
            index += 1
            while index < length:
                if document[index] == "\\":
                    index += 2
                elif document[index] == '"':
                    index += 1
                    break
                else:
                    index += 1
            continue
        if char == "{":
            depth += 1
            at_definition_start = False
            index += 1
            continue
        if char == "}":
            depth = max(0, depth - 1)
            if depth == 0:
                at_definition_start = True
            index += 1
            continue
        if char.isalpha() or char == "_":
            end = index + 1
            while end < length and (document[end].isalnum() or document[end] == "_"):
                end += 1
            if depth == 0 and at_definition_start:
                names.append(document[index:end].lower())
                at_definition_start = False
            index = end
            continue
        index += 1
    return names


def assert_read_only_graphql(query: str) -> str:
    """Refuse a GraphQL document whose operation is not a plain query.

    Raises:
        ValueError: When the document opens with ``mutation`` or
            ``subscription`` (a verifier must never write), or is empty.
    """
    if not query or not query.strip():
        raise ValueError("GraphQL effect verifier requires a non-empty query")
    if any(
        name in {"mutation", "subscription"} for name in _root_definition_names(query)
    ):
        raise ValueError(
            "GraphQL effect verifier accepts READ-ONLY query operations only "
            "(got a mutation/subscription document) -- a verifier must never "
            "write; run it under a read-only API credential regardless"
        )
    return query


def extract_records_path(body: Any, dotted: str) -> Optional[list[dict[str, Any]]]:
    """Extract the records list at ``dotted`` (``data.loans.nodes``) from a
    parsed GraphQL response body.

    Returns ``None`` -- read as unreadable -- when any segment is missing or
    the destination is not a list of objects.
    """
    node: Any = body
    for seg in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(seg)
    if not isinstance(node, list):
        return None
    if not all(isinstance(item, dict) for item in node):
        return None
    return [dict(item) for item in node]


class GraphQLRecordVerifier(VerifierAdapterBase):
    """Verify effects against a GraphQL system of record (read-back query).

    Args:
        endpoint: Full GraphQL endpoint URL (e.g. ``https://sor/graphql``).
        query: The read-only query document returning the candidate records.
        variables: Resolved query variables (entity/tenant binding -- the
            deployment layer resolves ``{param: ...}`` references into these
            before construction).
        records_path: Dotted path to the records LIST in the response body
            (default ``data`` expects the operation to select a top-level
            list; use e.g. ``data.loans.nodes`` for connection shapes).
        session: Optional ``requests``-style session (injectable for tests /
            custom transports).
        headers: Secret-isolated auth headers (``AuthRef.resolve_headers``).
        timeout_s: Per-request timeout.
        poll_interval_s: Settlement poll gap within ``Effect.timeout_s``.
        freshness_field: Record field carrying an ISO-8601 / epoch timestamp;
            with ``freshness_window_s`` enables the STALE demotion.
        freshness_window_s: Maximum evidence age in seconds.
        redaction: Field-level evidence-minimization policy.
        collateral_hooks: Substrate-specific collateral-effect checks.
    """

    substrate = "graphql"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(
        self,
        endpoint: str,
        *,
        query: str,
        variables: Optional[dict[str, Any]] = None,
        records_path: str = "data",
        session: Any = None,
        headers: Optional[dict[str, str]] = None,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.2,
        freshness_field: Optional[str] = None,
        freshness_window_s: Optional[float] = None,
        redaction: Optional[RedactionPolicy] = None,
        collateral_hooks: tuple[CollateralHook, ...] = (),
    ) -> None:
        self.endpoint = endpoint
        self.query = assert_read_only_graphql(query)
        self.variables = dict(variables or {})
        self.records_path = records_path
        self.headers = dict(headers) if headers else None
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self.freshness_field = freshness_field
        self.freshness_window_s = freshness_window_s
        self.redaction = redaction
        self.collateral_hooks = collateral_hooks
        self._session = session

    # -- transport ----------------------------------------------------------

    def _get_session(self) -> Any:
        """Return the read client, preferring an injected one.

        ``requests`` is used when present (unchanged for every deployment that
        already has it) and ``httpx`` otherwise. ``httpx`` is a core dependency
        while ``requests`` is only a dev/``windows`` extra, so without this
        fallback a clean wheel install could not read a GraphQL system of record
        at all: every verify raised inside :meth:`_fetch_records`, was read as
        *unreadable*, and HALTed. That failed safe -- it never invented a
        confirmation -- but it made ``effects.kind: graphql`` unusable out of the
        box. Both clients expose the same ``post(url, json=..., headers=...,
        timeout=...)`` surface and the same ``status_code`` / ``json()`` on the
        response.
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

    def _fetch_records(self) -> Optional[list[dict[str, Any]]]:
        """POST the query and extract the records list.

        Returns ``None`` -- unreadable, forcing INDETERMINATE (UNAVAILABLE)
        -- on any transport error, non-2xx status, unparseable body,
        non-empty ``errors`` array, or shape mismatch. Never raises.
        """
        payload = {"query": self.query, "variables": self.variables}
        try:
            if self.headers:
                resp = self._get_session().post(
                    self.endpoint,
                    json=payload,
                    timeout=self.timeout_s,
                    headers=self.headers,
                )
            else:
                resp = self._get_session().post(
                    self.endpoint, json=payload, timeout=self.timeout_s
                )
        except Exception:  # noqa: BLE001 - any transport failure is unreadable
            return None
        if resp.status_code // 100 != 2:
            return None
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 - unparseable body is unreadable
            return None
        if not isinstance(body, dict):
            return None
        # GraphQL reports auth/validation failures as `errors` with HTTP 200:
        # a partial or errored response is never trusted as the record set.
        if body.get("errors"):
            return None
        return extract_records_path(body, self.records_path)

    # -- VerifierAdapter lifecycle ------------------------------------------

    def capture_pre_state(self, context: Any = None) -> EffectState:
        records = self._fetch_records()
        return EffectState(
            substrate=self.substrate,
            reachable=records is not None,
            records=records or [],
            detail={"endpoint": self.endpoint, "records_path": self.records_path},
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        verdict = poll_until_settled(
            self._fetch_records,
            expected,
            before,
            substrate=self.substrate,
            poll_interval_s=self.poll_interval_s,
        )
        if self.freshness_field is not None and self.freshness_window_s is not None:
            verdict = enforce_freshness(
                verdict,
                freshness_field=self.freshness_field,
                window_s=self.freshness_window_s,
            )
        if self.collateral_hooks:
            verdict = apply_collateral_hooks(
                verdict, before, self.capture_post_state(context), self.collateral_hooks
            )
        return redact_verdict(verdict, self.redaction, field=expected.field)
