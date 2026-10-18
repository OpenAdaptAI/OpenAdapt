"""Resolve look-alike identifier abstains against a complete known-ID list.

The OCR identity tier abstains when the band rests on an identifier carrying
a look-alike glyph (O/0, l/1/I, B/8, S/5, Z/2, G/9). It must: OCR can read
``100512`` and a different record's ``1OO512`` as the same string, so a
matched name and date of birth cannot rule out a look-alike twin.

On pixel-only surfaces (remote desktop, VDI) that abstain also stops the
correct record whenever its own identifier happens to contain one of those
glyphs. This module removes that cost in one narrow case: the operator
supplies the COMPLETE list of identifiers that exist in the target system,
from a channel other than the screen being checked (for example an export of
the account or record table). If, in that closed list, exactly one
identifier falls in the look-alike class OCR read, no look-alike twin exists
anywhere in the system, so the abstain can be lifted.

Rules, all required:

- the list is attested complete for the identifier namespace;
- at least :data:`MIN_READS` independent OCR reads of the live band all
  abstained for the look-alike reason only (every other identity budget,
  such as name and date of birth, passed in every read);
- every read yields the same set of look-alike classes for its
  identifier-shaped tokens;
- each of those classes has exactly one member in the list;
- in parameter mode, a run value shaped like an identifier must be that
  member.

Anything else leaves the original verdict unchanged. This step can only turn
``abstain`` into ``verified``; it never touches ``mismatch`` or
``unreadable``, and it makes no model calls.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from openadapt_flow.ir import IdentityCheck, KnownIdEvidence
from openadapt_flow.runtime import identity as identity_mod

MIN_READS = 2
"""Independent OCR reads of the live band required before a lift."""

ABSTAIN_REASON_GLYPH = "glyph_confusable_identifier"


class KnownIdListError(ValueError):
    """The known-ID list cannot be used to resolve identity."""


def id_core(token: str) -> str:
    """Identifier comparison form: lowercase, no whitespace or separators."""
    return identity_mod._ID_SEP_RE.sub("", identity_mod.squash(token))


def lookalike_class(token: str) -> str:
    """Key shared by every identifier OCR could read as ``token``."""
    return identity_mod.ocr_canonical(id_core(token))


@dataclass(frozen=True)
class KnownIdList:
    """A complete, closed list of identifiers in one namespace.

    Attributes:
        ids: The identifiers, as the system of record stores them.
        source: Where the list came from (shown in reports), for example
            ``"accounts table export 2026-10-17"``.
        complete: The operator attests the list holds EVERY identifier in
            the namespace. An incomplete list cannot rule out a twin, so
            :func:`resolve_lookalike_abstain` refuses to use one.
    """

    ids: tuple[str, ...]
    source: str
    complete: bool
    _classes: dict[str, tuple[str, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        classes: dict[str, list[str]] = {}
        seen: set[str] = set()
        for raw in self.ids:
            core = id_core(raw)
            if not core or core in seen:
                continue
            seen.add(core)
            classes.setdefault(identity_mod.ocr_canonical(core), []).append(core)
        object.__setattr__(
            self, "_classes", {k: tuple(sorted(v)) for k, v in classes.items()}
        )

    @classmethod
    def load(cls, path: Path | str) -> "KnownIdList":
        """Load ``{"source": str, "complete": true, "ids": [str, ...]}``."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("ids"), list):
            raise KnownIdListError("known-ID list must be an object with an 'ids' list")
        if data.get("complete") is not True:
            raise KnownIdListError(
                'known-ID list must set "complete": true; an incomplete list '
                "cannot rule out a look-alike twin"
            )
        source = data.get("source")
        if not isinstance(source, str) or not source.strip():
            raise KnownIdListError("known-ID list must name its 'source'")
        ids = [str(v) for v in data["ids"]]
        return cls(ids=tuple(ids), source=source, complete=True)

    def members(self, token: str) -> tuple[str, ...]:
        """Listed identifiers OCR could read as ``token``."""
        return self._classes.get(lookalike_class(token), ())

    @property
    def size(self) -> int:
        return sum(len(v) for v in self._classes.values())

    @property
    def sha256(self) -> str:
        """Digest of the normalized list, recorded in each lifted verdict."""
        body = "\n".join(sorted(c for v in self._classes.values() for c in v))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _identifier_tokens(text: str) -> list[str]:
    return [
        tok
        for tok in (identity_mod.squash(t) for t in text.split())
        if tok and identity_mod._is_identifier_shaped(tok)
    ]


def resolve_lookalike_abstain(
    reads: Sequence[IdentityCheck],
    known_ids: Optional[KnownIdList],
    *,
    run_values: Optional[Mapping[str, str]] = None,
    min_reads: int = MIN_READS,
) -> Optional[IdentityCheck]:
    """Lift a look-alike abstain to ``verified`` when the known-ID list allows.

    Args:
        reads: One :class:`IdentityCheck` per independent OCR read of the
            live band, each produced by the normal OCR tier.
        known_ids: The complete known-ID list, or None.
        run_values: Run parameter values (parameter mode).
        min_reads: Reads required; :data:`MIN_READS` in production.

    Returns:
        A ``verified`` check carrying :class:`KnownIdEvidence`, or None when
        any rule fails (the caller keeps its original verdict).
    """
    if known_ids is None or not known_ids.complete:
        return None
    if len(reads) < max(1, min_reads):
        return None
    if any(
        r.status != "abstain" or r.abstain_reason != ABSTAIN_REASON_GLYPH for r in reads
    ):
        return None

    class_sets: list[frozenset[str]] = []
    for r in reads:
        toks = _identifier_tokens(r.observed)
        if not toks:
            return None
        class_sets.append(frozenset(lookalike_class(t) for t in toks))
    if any(s != class_sets[0] for s in class_sets[1:]):
        return None

    resolved: list[str] = []
    for key in sorted(class_sets[0]):
        members = known_ids._classes.get(key, ())
        if len(members) != 1:
            return None
        resolved.append(members[0])

    for value in (run_values or {}).values():
        if not isinstance(value, str):
            continue
        for tok in _identifier_tokens(value):
            if lookalike_class(tok) in class_sets[0] and id_core(tok) not in resolved:
                return None

    base = reads[0]
    return base.model_copy(
        update={
            "status": "verified",
            "abstain_reason": None,
            "known_id_evidence": KnownIdEvidence(
                list_sha256=known_ids.sha256,
                list_source=known_ids.source,
                list_size=known_ids.size,
                reads=len(reads),
                resolved_ids=len(resolved),
            ),
        }
    )


def ids_from_rows(rows: Iterable[Mapping[str, object]], column: str) -> list[str]:
    """Collect one column of a worklist or export as identifiers."""
    return [str(r[column]) for r in rows if r.get(column) not in (None, "")]
