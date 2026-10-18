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

- the list comes from a configured export of the system of record, and an
  operator confirmed its source and completeness at admission (see
  :func:`confirm_export` and :class:`KnownIdAdmission`); replay halts when
  that confirmation is missing, the export's hash changed, or the export is
  older than the configured age;
- at least :data:`MIN_READS` OCR reads of the live band, each from its own
  screenshot (the replayer captures a fresh frame for the second read), all
  abstained for the look-alike reason only (every other identity budget,
  such as name and date of birth, passed in every read);
- every read yields the same set of look-alike classes for its
  identifier-shaped tokens;
- each of those classes has exactly one member in the list;
- in parameter mode, a run value shaped like an identifier must be that
  member;
- otherwise (recorded-identity mode) the recorded band must hold that exact
  member. A stale list can hold only a look-alike of the recorded identifier
  (the record was added after the export); resolving to that look-alike
  would verify the wrong record.

Anything else leaves the original verdict unchanged. This step can only turn
``abstain`` into ``verified``; it never touches ``mismatch`` or
``unreadable``, and it makes no model calls.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Iterable,
    Literal,
    Mapping,
    Optional,
    Sequence,
)

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
    recorded_id_matches: Optional[Callable[[str], bool]] = None,
    min_reads: int = MIN_READS,
) -> Optional[IdentityCheck]:
    """Lift a look-alike abstain to ``verified`` when the known-ID list allows.

    Args:
        reads: One :class:`IdentityCheck` per independent OCR read of the
            live band, each produced by the normal OCR tier.
        known_ids: The complete known-ID list, or None.
        run_values: Run parameter values (parameter mode).
        recorded_id_matches: Whether a resolved member (in :func:`id_core`
            form) is an identifier of the recorded band. Needed when the
            recorded band is a hashed template; by default the identifier
            tokens of ``reads[0].expected`` are used. A member that no run
            value names must match the recorded band.
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

    named_by_run: set[str] = set()
    for value in (run_values or {}).values():
        if not isinstance(value, str):
            continue
        for tok in _identifier_tokens(value):
            if lookalike_class(tok) in class_sets[0]:
                if id_core(tok) not in resolved:
                    return None
                named_by_run.add(id_core(tok))

    if recorded_id_matches is None:
        recorded = {id_core(t) for t in _identifier_tokens(reads[0].expected)}
        recorded_id_matches = recorded.__contains__
    for member in resolved:
        if member not in named_by_run and not recorded_id_matches(member):
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


# --------------------------------------------------------------- admission
#
# The list must come from a configured export of the system of record, and a
# person must confirm, at admission, where it came from and that it is
# complete. That confirmation is recorded (who, when, source, row count,
# hash). Replay refuses to use the list, and halts with a named reason, when
# there is no confirmation, when the export's bytes no longer match the
# confirmed hash, or when the export is older than the configured age.

CONFIRMATION_SCHEMA = "openadapt.known-ids-confirmation/v1"
DEFAULT_MAX_AGE_S = 24 * 3600.0

KnownIdHaltReason = Literal[
    "known_ids_unconfirmed", "known_ids_changed", "known_ids_stale"
]


class KnownIdAdmissionHalt(KnownIdListError):
    """The known-ID list may not be used; replay must halt."""

    def __init__(self, reason: KnownIdHaltReason, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason: KnownIdHaltReason = reason
        self.detail = detail


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def file_sha256(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_export(path: Path | str, column: Optional[str] = None) -> list[str]:
    """Identifiers from an export of the system of record.

    Accepts CSV (``column`` names the identifier column), a JSON list, a JSON
    object with an ``ids`` list, or plain text with one identifier per line.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(text)
        ids = data.get("ids") if isinstance(data, dict) else data
        if not isinstance(ids, list):
            raise KnownIdListError(f"{path}: expected a list of identifiers")
        return [str(v) for v in ids if v not in (None, "")]
    if suffix in (".csv", ".tsv"):
        if not column:
            raise KnownIdListError(f"{path}: name the identifier column")
        reader = csv.DictReader(
            io.StringIO(text), delimiter="\t" if suffix == ".tsv" else ","
        )
        if reader.fieldnames is None or column not in reader.fieldnames:
            raise KnownIdListError(f"{path}: no column {column!r}")
        return ids_from_rows(reader, column)
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


@dataclass(frozen=True)
class KnownIdConfirmation:
    """What the operator confirmed at admission."""

    confirmed_by: str
    confirmed_at: str
    source: str
    export_path: str
    exported_at: str
    row_count: int
    sha256: str
    id_column: Optional[str]
    complete: bool = True
    schema: str = CONFIRMATION_SCHEMA

    def wire(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def load(cls, path: Path | str) -> "KnownIdConfirmation":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != CONFIRMATION_SCHEMA:
            raise KnownIdListError(f"{path}: not a {CONFIRMATION_SCHEMA} record")
        try:
            return cls(**data)
        except TypeError as exc:
            raise KnownIdListError(f"{path}: bad confirmation record") from exc


def default_confirmation_path(export_path: Path | str) -> Path:
    p = Path(export_path)
    return p.with_name(p.name + ".confirmation.json")


def confirm_export(
    export_path: Path | str,
    *,
    source: str,
    confirmed_by: str,
    column: Optional[str] = None,
    exported_at: Optional[datetime] = None,
    confirmation_path: Optional[Path | str] = None,
    now: Optional[datetime] = None,
) -> KnownIdConfirmation:
    """Record that ``confirmed_by`` confirmed this export complete.

    Writes the confirmation next to the export (or at ``confirmation_path``)
    and appends it to ``<confirmation>.log.jsonl`` so earlier confirmations
    stay on record. ``exported_at`` defaults to the file's modification time.
    """
    if not source.strip():
        raise KnownIdListError("name the export's source")
    if not confirmed_by.strip():
        raise KnownIdListError("record who confirmed the list")
    export_path = Path(export_path)
    ids = load_export(export_path, column)
    if not ids:
        raise KnownIdListError(f"{export_path}: no identifiers")
    exported = exported_at or datetime.fromtimestamp(
        export_path.stat().st_mtime, tz=timezone.utc
    )
    record = KnownIdConfirmation(
        confirmed_by=confirmed_by.strip(),
        confirmed_at=_iso(now or _utc_now()),
        source=source.strip(),
        export_path=str(export_path.resolve()),
        exported_at=_iso(exported),
        row_count=len(ids),
        sha256=file_sha256(export_path),
        id_column=column,
    )
    out = Path(confirmation_path or default_confirmation_path(export_path))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(record.wire(), indent=2, sort_keys=True) + "\n", "utf-8")
    os.replace(tmp, out)
    with open(out.with_name(out.name + ".log.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record.wire(), sort_keys=True) + "\n")
    return record


@dataclass
class KnownIdAdmission:
    """A confirmed known-ID list, re-checked before every use."""

    export_path: Path
    confirmation: KnownIdConfirmation
    known_ids: KnownIdList
    max_age_s: float = DEFAULT_MAX_AGE_S

    @classmethod
    def load(
        cls,
        export_path: Path | str,
        *,
        column: Optional[str] = None,
        confirmation_path: Optional[Path | str] = None,
        max_age_s: float = DEFAULT_MAX_AGE_S,
        now: Optional[datetime] = None,
    ) -> "KnownIdAdmission":
        """Load and check. Raises :class:`KnownIdAdmissionHalt` if unusable."""
        export_path = Path(export_path)
        cpath = Path(confirmation_path or default_confirmation_path(export_path))
        if not cpath.is_file():
            raise KnownIdAdmissionHalt(
                "known_ids_unconfirmed",
                f"no admission confirmation at {cpath}; run "
                "`openadapt-flow known-ids confirm`",
            )
        confirmation = KnownIdConfirmation.load(cpath)
        admission = cls(
            export_path=export_path,
            confirmation=confirmation,
            known_ids=KnownIdList(ids=(), source=confirmation.source, complete=False),
            max_age_s=max_age_s,
        )
        halt = admission.check(now)
        if halt is not None:
            raise halt
        ids = load_export(export_path, column or confirmation.id_column)
        admission.known_ids = KnownIdList(
            ids=tuple(ids), source=confirmation.source, complete=confirmation.complete
        )
        return admission

    def check(self, now: Optional[datetime] = None) -> Optional[KnownIdAdmissionHalt]:
        """None when the list may be used now; otherwise the halt to raise."""
        c = self.confirmation
        if not c.complete:
            return KnownIdAdmissionHalt(
                "known_ids_unconfirmed", "completeness was not confirmed"
            )
        try:
            digest = file_sha256(self.export_path)
        except OSError:
            return KnownIdAdmissionHalt(
                "known_ids_changed", f"export {self.export_path} is unreadable"
            )
        if digest != c.sha256:
            return KnownIdAdmissionHalt(
                "known_ids_changed",
                "the export changed since it was confirmed "
                f"({c.sha256[:12]} -> {digest[:12]}); confirm it again",
            )
        age = ((now or _utc_now()) - _parse_iso(c.exported_at)).total_seconds()
        if age > self.max_age_s:
            return KnownIdAdmissionHalt(
                "known_ids_stale",
                f"the export is {age / 3600:.1f} h old; the limit is "
                f"{self.max_age_s / 3600:.1f} h. Load a fresh export and confirm it",
            )
        return None
