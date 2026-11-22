"""When a ``tx`` API read-back counts as a system-of-record read.

The ``tx`` engine (:mod:`openadapt_flow.tx.lens`) trusts an atomic SQLite row
read (basis ``owned_connection``). Most customers give no database access, so
an API read of the record can also count, as check level 4 with basis
``system_api``, but only when all of these hold:

- the operation is an update, so there is a before-image to compare with;
- the read asked to bypass caches (:data:`CACHE_BYPASS_HEADERS`) and the
  response shows no sign of a cache (:func:`served_from_cache`);
- the record carries an integer version that is exactly one more than the
  before-image's version, so this read saw this write and no other;
- nothing reported a concurrent modification.

Anything else is not a system-of-record read and the verdict stays
``UNCERTAIN``. The wire tap never substitutes for the read: it shows what was
sent, not what the system stored.

:func:`tx_evidence_scope` builds the receipt's evidence-scope block
(:mod:`openadapt_types.evidence_scope`) with the same ladder as every other
path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from openadapt_types.check_level import Basis, CheckLevel
from openadapt_types.evidence_scope import build_evidence_scope

__all__ = [
    "CACHE_BYPASS_HEADERS",
    "api_readback_basis",
    "served_from_cache",
    "tx_evidence_scope",
]

#: Sent on every ``tx`` record read that is not an atomic row read.
CACHE_BYPASS_HEADERS: Mapping[str, str] = {
    "Cache-Control": "no-cache, no-store, max-age=0",
    "Pragma": "no-cache",
}

#: Response headers that report a cache, and the values that mean a hit.
_CACHE_STATUS_HEADERS = (
    "x-cache",
    "x-cache-status",
    "cf-cache-status",
    "x-proxy-cache",
    "x-varnish-cache",
    "cdn-cache",
)
_CACHE_HIT_WORDS = ("hit", "stale", "revalidated", "updating")


def served_from_cache(
    headers: Optional[Mapping[str, str]], status: Optional[int]
) -> bool:
    """True when the response may have come from a cache (fails closed)."""

    if status == 304:
        return True
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    age = lowered.get("age")
    if age is not None and age.strip() not in ("", "0"):
        return True
    if "warning" in lowered and lowered["warning"].strip().startswith("11"):
        return True  # 110 / 111 / 112 / 113: a stale or heuristic cache.
    for name in _CACHE_STATUS_HEADERS:
        value = lowered.get(name, "").lower()
        if any(word in value for word in _CACHE_HIT_WORDS):
            return True
    return False


def _int_version(record: Optional[Mapping[str, Any]], field: str) -> Optional[int]:
    if record is None or field not in record:
        return None
    value = record[field]
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def api_readback_basis(
    *,
    operation: str,
    r0: Optional[Mapping[str, Any]],
    r1: Optional[Mapping[str, Any]],
    headers: Optional[Mapping[str, str]],
    status: Optional[int],
    version_field: Optional[str],
    occ_status: str,
    cache_bypassed: bool,
) -> Optional[Basis]:
    """``system_api`` when the post-commit API read qualifies; else None."""

    if operation != "update" or not cache_bypassed or status != 200:
        return None
    if occ_status == "CONCURRENT_MODIFICATION" or not version_field:
        return None
    if served_from_cache(headers, status):
        return None
    before = _int_version(r0, version_field)
    after = _int_version(r1, version_field)
    if before is None or after is None or after != before + 1:
        return None
    return "system_api"


def tx_evidence_scope(
    *,
    outcome: str,
    basis: Optional[Basis],
    read_back: bool,
    wire_observed: bool,
) -> dict[str, Any]:
    """The evidence-scope block of one ``tx`` commit.

    ``basis`` is ``owned_connection`` for an atomic row read, ``system_api``
    for a qualified API read and None otherwise. Only a ``CONFIRMED`` commit
    with a basis is ``VERIFIED``.
    """

    level = (
        CheckLevel.SYSTEM_OF_RECORD
        if basis is not None or read_back
        else CheckLevel.UNCHECKED
    )
    return build_evidence_scope(
        level,
        basis=basis,
        confirmed=outcome == "CONFIRMED" and basis is not None,
        wire_channel="http_proxy" if wire_observed else "none",
        protocols_parsed=("http",) if wire_observed else (),
        read_back_at=(
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if read_back
            else None
        ),
        not_covered=("other_records", "changes_after_read_back"),
    )
