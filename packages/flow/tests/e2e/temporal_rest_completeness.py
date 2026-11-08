"""Actual REST read facts projected into the single temporal test protocol."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, cast

import pytest

from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    Verdict,
)
from openadapt_flow.runtime.effects.rest import RestRecordVerifier, _dotted
from tests.e2e.temporal_observation_protocol import (
    ClaimEvidence,
    ClaimRef,
    Outcome,
    TargetState,
    TemporalObservation,
    VerdictSnapshot,
)


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _has_path(body: Any, path: str) -> bool:
    node = body
    for name in path.split("."):
        if not isinstance(node, dict) or name not in node:
            return False
        node = node[name]
    return True


def _count(value: Any) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


@dataclass(frozen=True)
class ActualPageFact:
    """Finite response facts; no rows, headers or response text are retained."""

    http_status: int | None
    returned_count: int | None
    server_total: int | None
    server_cap: int | None
    server_has_next: bool | None
    declared_total: int | None
    declared_next_present: bool
    declared_has_next: bool | None


@dataclass(frozen=True)
class ActualReadFact:
    pages: tuple[ActualPageFact, ...]
    returned_count: int | None
    read_problem: str | None
    complete: bool | None


@contextmanager
def capture_rest_reads(verifier: RestRecordVerifier) -> Iterator[list[ActualReadFact]]:
    """Delegate each actual GET/read once and preserve the exact return values.

    page.total/cap/next are this synthetic endpoint's independent native facts.
    Positive completeness additionally requires the reader's real configured
    total/next declaration; missing metadata is never guessed from page length.
    """
    pages: list[ActualPageFact] = []
    reads: list[ActualReadFact] = []
    original_json, original_fetch = verifier._get_json, verifier._fetch_records
    session = verifier._get_session()
    original_get = session.get
    responses: list[tuple[int | None, Any]] = []

    def get(*args: Any, **kwargs: Any) -> Any:
        response = original_get(*args, **kwargs)
        status = getattr(response, "status_code", None)
        status = (
            status if isinstance(status, int) and not isinstance(status, bool) else None
        )
        try:
            observed_body = json.loads(response.content)
        except (TypeError, ValueError):
            observed_body = None
        responses.append((status, observed_body))
        return response

    def get_json(url: str) -> Any:
        start = len(responses)
        body = original_json(url)
        status, observed_body = (
            responses[-1] if len(responses) == start + 1 else (None, body)
        )
        records = verifier._records_of(observed_body)
        metadata = (
            observed_body.get("page") if isinstance(observed_body, dict) else None
        )
        metadata = metadata if isinstance(metadata, dict) else {}
        raw_next = metadata.get("next")
        server_has_next = (
            bool(raw_next)
            if "next" in metadata and (raw_next is None or isinstance(raw_next, str))
            else None
        )
        next_present = verifier.next_path is not None and _has_path(
            observed_body, verifier.next_path
        )
        declared_next = (
            _dotted(observed_body, verifier.next_path)
            if verifier.next_path is not None
            else None
        )
        pages.append(
            ActualPageFact(
                http_status=status,
                returned_count=len(records) if records is not None else None,
                server_total=_count(metadata.get("total")),
                server_cap=_count(metadata.get("cap")),
                server_has_next=server_has_next,
                declared_total=_count(_dotted(observed_body, verifier.total_path))
                if verifier.total_path is not None
                else None,
                declared_next_present=next_present,
                declared_has_next=bool(declared_next)
                if next_present
                and (declared_next is None or isinstance(declared_next, str))
                else None,
            )
        )
        return body

    def fetch_records() -> Any:
        start = len(pages)
        current = original_fetch()
        chain = tuple(pages[start:])
        returned = len(current) if current is not None else None
        observed_count = sum(page.returned_count or 0 for page in chain)
        complete: bool | None = None
        if chain:
            # A declared server total or an unfollowed next link directly
            # establishes truncation; a full-looking row count alone does not.
            total = chain[0].server_total
            if (
                any(page.http_status == 206 for page in chain)
                or (total is not None and total != observed_count)
                or chain[-1].server_has_next is True
            ):
                complete = False
            elif current is not None:
                if (
                    verifier.total_path is not None
                    and chain[0].declared_total == returned
                ):
                    complete = True
                elif (
                    verifier.next_path is not None
                    and all(page.declared_next_present for page in chain)
                    and chain[-1].declared_has_next is False
                ):
                    complete = True
        reads.append(ActualReadFact(chain, returned, verifier.read_problem, complete))
        return current

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(session, "get", get)
        patch.setattr(verifier, "_get_json", get_json)
        patch.setattr(verifier, "_fetch_records", fetch_records)
        yield reads


def observe_rest_verdict(
    verifier: RestRecordVerifier,
    effect: Effect,
    before: EffectState,
    verdict: EffectVerdict,
    read: ActualReadFact,
) -> TemporalObservation:
    """Report the actual stateless reader outcome; no ledger/recheck is invented."""
    if effect.kind is not EffectKind.EXACT_NEW_SET or set(effect.match) != {"id"}:
        raise ValueError("the synthetic completeness observer requires one id selector")
    identity = effect.match["id"].resolve({})
    target_ref = _sha({"target": identity})
    declared = {
        "records_path": verifier.records_path,
        "records_key": verifier.records_key,
        "total_path": verifier.total_path,
        "next_path": verifier.next_path,
        "effect": effect.model_dump(mode="json"),
        "before": before.model_dump(mode="json"),
    }
    claim_id = _sha(declared)
    # The reader exposes no owned dispatch/admitted profile/authenticated
    # principal/source-tree proof. Empty references/None retain that limit.
    claim = ClaimRef(
        claim_id=claim_id,
        attempt_ref="",
        session_ref="",
        principal_ref=None,
        profile_ref="",
        source_tree_sha256="",
        operation="new_write",
        temporal_requirement="historical_commit",
        target_ref=target_ref,
        dependency_scope=None,
        expected_effect_sha256=_sha(effect.model_dump(mode="json")),
        window_start_ref=_sha(before.model_dump(mode="json")),
    )
    outcome = {
        Verdict.CONFIRMED: "confirmed",
        Verdict.REFUTED: "refuted",
        Verdict.INDETERMINATE: "uncertain",
    }[verdict.verdict]
    return TemporalObservation(
        phase_trace=("baseline_read", "settle"),
        begin_reached=True,
        mutations=(),
        write_count=0,
        verdicts=(
            VerdictSnapshot(
                phase="settle",
                transport_status=None,
                reply_state=None,
                reply_outcome=None,
                reply_verified=None,
                canonical_outcome=cast(Outcome, outcome),
                frame_verdict=None,
                durable_outcome=None,
                durable_state=None,
                claim_ref_id=claim_id,
            ),
        ),
        scope_witnesses=(),
        gaps=(),
        observed_dispatch_effect=None,
        verifier_attributed_effect=None,
        ledger_present=False,
        observed_target_state=cast(
            TargetState,
            verdict.observed_effect
            if verdict.observed_effect in ("absent", "present")
            else "unknown",
        ),
        claim_ref=claim,
        claim_evidence=(
            ClaimEvidence(
                phase="settle",
                claim_ref_id=claim_id,
                attempt_ref="",
                session_ref="",
                principal_ref=None,
                source_tree_sha256="",
                channel=None,
                profile_ref=None,
                profile_admitted=None,
                semantics_sha256=None,
                owned_exchange_ref=None,
                request_ref=None,
                response_ref=None,
                effect_disposition=None,
                effect_target_ref=None,
                effect_version_ref=None,
                native_operation_ref=None,
                readback_semantics="authoritative_current",
                readback_target_ref=target_ref,
                readback_version_ref=None,
                readback_effect_sha256=None,
                readback_freshness="authoritative"
                if read.complete is True
                else "unknown",
                readback_complete=read.complete,
                window_complete=None,
            ),
        ),
    )
