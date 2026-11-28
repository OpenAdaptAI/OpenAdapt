"""PAGINATE_UNTIL_EOF completeness primitive with cycle and count reconciliation.

Ensures paginated table extraction terminates safely and proves completeness:
- Tracks per-page row-key hashes and unique-key monotonicity to detect loops or
  stalled pagination controls.
- Deduplicates overlapping rows across pages using declared composite keys.
- Parses visible header totals (for example ``"1-20 of 47"``) and returns
  ``RECONCILIATION_REQUIRED`` when collected unique rows do not match the
  declared total.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Sequence

__all__ = [
    "HeaderCount",
    "PageSnapshot",
    "PaginationResult",
    "PaginationStatus",
    "compute_page_hash",
    "extract_composite_key",
    "paginate_until_eof",
    "parse_visible_header_count",
]


class PaginationStatus(str, Enum):
    COMPLETE = "COMPLETE"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    CYCLE_DETECTED = "CYCLE_DETECTED"
    MAX_PAGES_EXCEEDED = "MAX_PAGES_EXCEEDED"


@dataclass(frozen=True)
class HeaderCount:
    """Parsed visible pagination header count (e.g. ``1-20 of 47``)."""

    total: int
    raw_text: str
    start: Optional[int] = None
    end: Optional[int] = None


@dataclass(frozen=True)
class PageSnapshot:
    """One fetched page of extracted rows and optional header metadata."""

    rows: Sequence[Mapping[str, Any]]
    header_text: Optional[str] = None
    has_next: Optional[bool] = None
    page_index: Optional[int] = None


@dataclass(frozen=True)
class PaginationResult:
    """Outcome of executing PAGINATE_UNTIL_EOF across a paginated list."""

    status: PaginationStatus
    rows: tuple[dict[str, Any], ...]
    pages_visited: int
    duplicate_rows_skipped: int
    expected_total: Optional[int]
    observed_total: int
    page_hashes: tuple[str, ...]
    reason: Optional[str] = None


_RANGE_OF_TOTAL = re.compile(
    r"\b(\d[\d,]*)\s*(?:-|to)\s*(\d[\d,]*)\s+of\s+(\d[\d,]*)\b",
    re.IGNORECASE,
)
_TOTAL_ONLY = re.compile(
    r"(?:\btotal\s*[:=]\s*(\d[\d,]*)\b|\b(\d[\d,]*)\s+(?:results?|items?|records?|rows?|entries)\b)",
    re.IGNORECASE,
)


def _parse_int(token: str) -> int:
    return int(token.replace(",", ""))


def parse_visible_header_count(header_text: Optional[str]) -> Optional[HeaderCount]:
    """Parse a visible pagination count header such as ``1-20 of 47``."""
    if not header_text:
        return None
    text = " ".join(header_text.split())
    if not text:
        return None

    m_range = _RANGE_OF_TOTAL.search(text)
    if m_range:
        return HeaderCount(
            start=_parse_int(m_range.group(1)),
            end=_parse_int(m_range.group(2)),
            total=_parse_int(m_range.group(3)),
            raw_text=text,
        )

    m_total = _TOTAL_ONLY.search(text)
    if m_total:
        raw_num = m_total.group(1) or m_total.group(2)
        if raw_num is not None:
            return HeaderCount(total=_parse_int(raw_num), raw_text=text)

    return None


def _normalize_key_part(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).casefold()


def extract_composite_key(
    row: Mapping[str, Any], key_fields: Sequence[str]
) -> tuple[str, ...]:
    """Build a normalized composite key tuple for ``row`` from ``key_fields``."""
    if not key_fields:
        raise ValueError("key_fields must contain at least one field name")
    return tuple(_normalize_key_part(row.get(field)) for field in key_fields)


def compute_page_hash(row_keys: Sequence[tuple[str, ...]]) -> str:
    """Compute a deterministic SHA-256 digest of a page's ordered row keys."""
    payload = json.dumps(list(row_keys), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def paginate_until_eof(
    fetch_page: Callable[[int], Optional[PageSnapshot]],
    *,
    key_fields: Sequence[str],
    max_pages: int = 100,
) -> PaginationResult:
    """Iterate pages until EOF with cycle detection, dedup, and total reconciliation.

    ``fetch_page(page_index)`` is called with 0-based page indices (0, 1, 2, ...).
    Returning ``None`` indicates no further page exists.
    """
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    if not key_fields:
        raise ValueError("key_fields must contain at least one field name")

    seen_keys: set[tuple[str, ...]] = set()
    seen_page_hashes: set[str] = set()
    page_hashes: list[str] = []
    collected: list[dict[str, Any]] = []
    duplicate_rows_skipped = 0
    expected_total: Optional[int] = None
    pages_visited = 0

    for page_index in range(max_pages):
        snapshot = fetch_page(page_index)
        if snapshot is None:
            break
        pages_visited += 1

        parsed_header = parse_visible_header_count(snapshot.header_text)
        if parsed_header is not None:
            expected_total = parsed_header.total

        row_keys = [extract_composite_key(row, key_fields) for row in snapshot.rows]
        page_hash = compute_page_hash(row_keys)

        if page_hash in seen_page_hashes and snapshot.rows:
            return PaginationResult(
                status=PaginationStatus.CYCLE_DETECTED,
                rows=tuple(collected),
                pages_visited=pages_visited,
                duplicate_rows_skipped=duplicate_rows_skipped,
                expected_total=expected_total,
                observed_total=len(collected),
                page_hashes=tuple(page_hashes),
                reason=f"repeated page row-key hash on page {page_index}",
            )

        seen_page_hashes.add(page_hash)
        page_hashes.append(page_hash)

        new_rows_on_page = 0
        for row, key in zip(snapshot.rows, row_keys):
            if key in seen_keys:
                duplicate_rows_skipped += 1
                continue
            seen_keys.add(key)
            collected.append(dict(row))
            new_rows_on_page += 1

        # Monotonicity check: after page 0, a non-empty page with zero new keys
        # means pagination did not advance onto unseen rows.
        if page_index > 0 and snapshot.rows and new_rows_on_page == 0:
            return PaginationResult(
                status=PaginationStatus.CYCLE_DETECTED,
                rows=tuple(collected),
                pages_visited=pages_visited,
                duplicate_rows_skipped=duplicate_rows_skipped,
                expected_total=expected_total,
                observed_total=len(collected),
                page_hashes=tuple(page_hashes),
                reason=f"no new unique row keys on page {page_index}",
            )

        if snapshot.has_next is False:
            break
        if not snapshot.rows and snapshot.has_next is not True:
            break
    else:
        # Loop exhausted max_pages without explicit EOF.
        return PaginationResult(
            status=PaginationStatus.MAX_PAGES_EXCEEDED,
            rows=tuple(collected),
            pages_visited=pages_visited,
            duplicate_rows_skipped=duplicate_rows_skipped,
            expected_total=expected_total,
            observed_total=len(collected),
            page_hashes=tuple(page_hashes),
            reason=f"reached max_pages={max_pages} before EOF",
        )

    observed_total = len(collected)
    if expected_total is not None and observed_total != expected_total:
        return PaginationResult(
            status=PaginationStatus.RECONCILIATION_REQUIRED,
            rows=tuple(collected),
            pages_visited=pages_visited,
            duplicate_rows_skipped=duplicate_rows_skipped,
            expected_total=expected_total,
            observed_total=observed_total,
            page_hashes=tuple(page_hashes),
            reason=(
                f"visible header reported {expected_total} rows, "
                f"but collected {observed_total} unique rows"
            ),
        )

    return PaginationResult(
        status=PaginationStatus.COMPLETE,
        rows=tuple(collected),
        pages_visited=pages_visited,
        duplicate_rows_skipped=duplicate_rows_skipped,
        expected_total=expected_total,
        observed_total=observed_total,
        page_hashes=tuple(page_hashes),
        reason=None,
    )
