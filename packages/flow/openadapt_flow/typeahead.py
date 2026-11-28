"""Async type-ahead secondary-key disambiguator.

Matches type-ahead dropdown candidates using a primary key plus one or more
secondary keys (for example patient name + DOB or MRN) and halts cleanly on
ties, zero matches, or unsettled async suggestion lists.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

__all__ = [
    "DisambiguationResult",
    "DisambiguationStatus",
    "TypeAheadCandidate",
    "disambiguate_async",
    "disambiguate_candidates",
]


class DisambiguationStatus(str, Enum):
    SELECTED = "SELECTED"
    HALT_NO_MATCH = "HALT_NO_MATCH"
    HALT_AMBIGUOUS_TIE = "HALT_AMBIGUOUS_TIE"
    HALT_TIMEOUT = "HALT_TIMEOUT"


@dataclass(frozen=True)
class TypeAheadCandidate:
    """One visible option in an async type-ahead suggestion list."""

    label: str
    fields: Mapping[str, Any] = field(default_factory=dict)
    candidate_id: Optional[str] = None
    index: int = 0


@dataclass(frozen=True)
class DisambiguationResult:
    """Outcome of matching candidates against primary and secondary keys."""

    status: DisambiguationStatus
    selected: Optional[TypeAheadCandidate]
    matched_candidates: tuple[TypeAheadCandidate, ...]
    attempts: int
    reason: str


_DATE_MDY = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_DATE_YMD = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


def _normalize_value(value: Any, *, key_name: str = "") -> str:
    text = " ".join(str(value).split()).strip()
    low_key = key_name.casefold()
    if any(tok in low_key for tok in ("dob", "birth", "date")):
        m_mdy = _DATE_MDY.match(text)
        if m_mdy:
            return f"{int(m_mdy.group(3)):04d}-{int(m_mdy.group(1)):02d}-{int(m_mdy.group(2)):02d}"
        m_ymd = _DATE_YMD.match(text)
        if m_ymd:
            return f"{int(m_ymd.group(1)):04d}-{int(m_ymd.group(2)):02d}-{int(m_ymd.group(3)):02d}"
    return text.casefold()


def _candidate_field_value(candidate: TypeAheadCandidate, key: str) -> Optional[str]:
    if key in candidate.fields and candidate.fields[key] is not None:
        return str(candidate.fields[key])
    low_key = key.casefold()
    for k, v in candidate.fields.items():
        if k.casefold() == low_key and v is not None:
            return str(v)
    if low_key == "label":
        return candidate.label
    return None


def _matches_key(
    candidate: TypeAheadCandidate,
    key: str,
    expected_value: Any,
    *,
    allow_label_fallback: bool = False,
) -> bool:
    expected = _normalize_value(expected_value, key_name=key)
    if not expected:
        return False
    observed_raw = _candidate_field_value(candidate, key)
    if observed_raw is not None:
        return _normalize_value(observed_raw, key_name=key) == expected
    if allow_label_fallback and candidate.label:
        return expected in _normalize_value(candidate.label, key_name=key)
    return False


def disambiguate_candidates(
    candidates: Sequence[TypeAheadCandidate],
    *,
    primary_key: str,
    primary_value: Any,
    secondary_keys: Mapping[str, Any],
    attempts: int = 1,
) -> DisambiguationResult:
    """Select the unique candidate matching primary and all secondary keys.

    Halts with ``HALT_NO_MATCH`` when zero candidates match and with
    ``HALT_AMBIGUOUS_TIE`` when two or more candidates match.
    """
    if not secondary_keys:
        raise ValueError("secondary_keys must provide at least one disambiguating key")

    primary_matches = [
        cand
        for cand in candidates
        if _matches_key(
            cand, primary_key, primary_value, allow_label_fallback=True
        )
    ]
    if not primary_matches:
        return DisambiguationResult(
            status=DisambiguationStatus.HALT_NO_MATCH,
            selected=None,
            matched_candidates=(),
            attempts=attempts,
            reason=f"no candidate matched primary key {primary_key!r}",
        )

    matched = [
        cand
        for cand in primary_matches
        if all(
            _matches_key(cand, skey, sval, allow_label_fallback=False)
            for skey, sval in secondary_keys.items()
        )
    ]

    if not matched:
        return DisambiguationResult(
            status=DisambiguationStatus.HALT_NO_MATCH,
            selected=None,
            matched_candidates=(),
            attempts=attempts,
            reason=(
                f"{len(primary_matches)} candidate(s) matched primary key "
                f"{primary_key!r}, but 0 matched secondary keys "
                f"{sorted(secondary_keys)}"
            ),
        )

    if len(matched) > 1:
        return DisambiguationResult(
            status=DisambiguationStatus.HALT_AMBIGUOUS_TIE,
            selected=None,
            matched_candidates=tuple(matched),
            attempts=attempts,
            reason=(
                f"ambiguous tie: {len(matched)} candidates matched primary key "
                f"{primary_key!r} and secondary keys {sorted(secondary_keys)}"
            ),
        )

    return DisambiguationResult(
        status=DisambiguationStatus.SELECTED,
        selected=matched[0],
        matched_candidates=(matched[0],),
        attempts=attempts,
        reason="unique primary and secondary key match",
    )


def _snapshot_signature(
    candidates: Sequence[TypeAheadCandidate],
) -> tuple[tuple[str, Optional[str], tuple[tuple[str, str], ...]], ...]:
    return tuple(
        (
            c.label,
            c.candidate_id,
            tuple(sorted((str(k), str(v)) for k, v in c.fields.items())),
        )
        for c in candidates
    )


async def disambiguate_async(
    fetch_candidates: Callable[
        [], Sequence[TypeAheadCandidate] | Awaitable[Sequence[TypeAheadCandidate]]
    ],
    *,
    primary_key: str,
    primary_value: Any,
    secondary_keys: Mapping[str, Any],
    timeout_s: float = 2.0,
    poll_interval_s: float = 0.05,
    settle_polls: int = 2,
) -> DisambiguationResult:
    """Poll an async type-ahead provider until candidates settle, then disambiguate.

    Waits until ``settle_polls`` consecutive non-empty polls return the same
    candidate snapshot, then runs :func:`disambiguate_candidates`. Returns
    ``HALT_TIMEOUT`` if suggestions never settle before ``timeout_s``.
    """
    if settle_polls < 1:
        raise ValueError("settle_polls must be at least 1")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")

    deadline = time.monotonic() + timeout_s
    attempts = 0
    last_sig: Any = None
    stable_count = 0
    latest: list[TypeAheadCandidate] = []

    while True:
        attempts += 1
        raw = fetch_candidates()
        if inspect.isawaitable(raw):
            raw = await raw
        latest = list(raw)
        sig = _snapshot_signature(latest)

        if latest and sig == last_sig:
            stable_count += 1
        elif latest:
            last_sig = sig
            stable_count = 1
        else:
            last_sig = None
            stable_count = 0

        if latest and stable_count >= settle_polls:
            return disambiguate_candidates(
                latest,
                primary_key=primary_key,
                primary_value=primary_value,
                secondary_keys=secondary_keys,
                attempts=attempts,
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_interval_s, remaining))

    return DisambiguationResult(
        status=DisambiguationStatus.HALT_TIMEOUT,
        selected=None,
        matched_candidates=(),
        attempts=attempts,
        reason=(
            f"type-ahead suggestions did not settle within {timeout_s:.2f}s "
            f"({attempts} poll(s), last count={len(latest)})"
        ),
    )
