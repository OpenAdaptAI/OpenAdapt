"""Two-law transactional state judge (Pierce Lens Frame Preservation + Intent Entailment).

Implements:
- Law 1 (Deterministic Frame Preservation): every non-volatile field outside
  ``intended_delta`` in ``R_0 U R_1`` must remain value-and-type identical on
  updates (catching partial-PUT clobbering, silent wipes, and bool/int coercion),
  or match benign server defaults on creates.
- Law 2 (Intended-Field Verification Ladder): Tier 0 exact match, Tier 1
  declarative ``FieldCodec`` / salted ``bijection_cache``, and Tier 2 opt-in
  ``EntailmentOracle``, guarded first by a hard numeric, calendar-date, and
  alphanumeric-identifier safety invariant.

Zero vision/GUI imports. Zero application names.
"""

from __future__ import annotations

import datetime
import fnmatch
import re
import secrets
import unicodedata
from collections.abc import Mapping, MutableMapping, MutableSet, Sequence
from dataclasses import dataclass
from typing import Any, Optional, Union

from openadapt_flow.tx.types import (
    EntailmentOracle,
    FieldCodec,
    FieldVerdict,
    JudgeProtocol,
    JudgeVerdict,
    salted_sha256,
)

DEFAULT_ALLOWED_SERVER_DEFAULTS: tuple[str, ...] = (
    "id",
    "uuid",
    "created_at",
    "createdAt",
    "status",
)

DEFAULT_ORACLE_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "_id",
        "uuid",
        "rank",
        "version",
        "versionId",
        "etag",
        "created_at",
        "createdAt",
        "updated_at",
        "updatedAt",
        "lastUpdated",
        "meta",
        "extension",
        "period",
    }
)

BijectionCache = Union[
    MutableSet[tuple[str, str]],
    MutableMapping[tuple[str, str], Any],
]

_MISSING = object()

_ISO_DATE_RE = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?"
    r"(Z|z|[+-]\d{2}:?\d{2})?)?\s*$"
)
_PHONE_CHARS_RE = re.compile(r"^\s*\+?[\d\s().-]+\s*$")
_NUMERIC_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?")
_IDENTIFIER_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_UPPER_CODE_RE = re.compile(r"^[A-Z]{1,4}$")
_NEGATION_WORDS: frozenset[str] = frozenset(
    {"no", "not", "none", "never", "without", "denies", "negative", "nil"}
)


@dataclass(frozen=True)
class _ParsedIsoDate:
    date: datetime.date
    has_time: bool
    hour: int = 0
    minute: int = 0
    second: int = 0
    microsecond: int = 0
    tz_offset: Optional[datetime.timedelta] = None

    @property
    def is_midnight(self) -> bool:
        return (
            self.hour == 0
            and self.minute == 0
            and self.second == 0
            and self.microsecond == 0
        )

    def as_utc_datetime(self) -> Optional[datetime.datetime]:
        if not self.has_time or self.tz_offset is None:
            return None
        tz = datetime.timezone(self.tz_offset)
        dt = datetime.datetime(
            self.date.year,
            self.date.month,
            self.date.day,
            self.hour,
            self.minute,
            self.second,
            self.microsecond,
            tzinfo=tz,
        )
        return dt.astimezone(datetime.timezone.utc)


def strict_equal(left: Any, right: Any) -> bool:
    """Exact value equality that never equates ``bool`` with ``int``/``float``."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left is right
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if left.keys() != right.keys():
            return False
        return all(strict_equal(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return False
        return all(strict_equal(x, y) for x, y in zip(left, right))
    if isinstance(left, tuple) and isinstance(right, tuple):
        if len(left) != len(right):
            return False
        return all(strict_equal(x, y) for x, y in zip(left, right))
    return bool(left == right)


def _is_empty_server_default_value(value: Any) -> bool:
    """Return True if ``value`` is a benign empty/zero default on record creation."""
    if value is None:
        return True
    if isinstance(value, bool):
        return value is False
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        return value == ""
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def matches_field_pattern(field_path: str, patterns: Sequence[str]) -> bool:
    """Return True if ``field_path`` matches any exact or glob pattern in ``patterns``."""
    for pattern in patterns:
        if pattern == field_path:
            return True
        if pattern.startswith("**.") and (
            field_path == pattern[3:]
            or fnmatch.fnmatchcase(field_path, f"*.{pattern[3:]}")
        ):
            return True
        if ("*" in pattern or "?" in pattern or "[" in pattern) and fnmatch.fnmatchcase(
            field_path, pattern
        ):
            return True
    return False


def _lookup_field_codec(
    field_path: str,
    field_codecs: Optional[Mapping[str, FieldCodec]],
) -> Optional[FieldCodec]:
    """Resolve an exact or glob-matched ``FieldCodec`` for ``field_path``."""
    if not field_codecs:
        return None
    direct = field_codecs.get(field_path)
    if direct is not None:
        return direct
    for pattern, codec in field_codecs.items():
        if ("*" in pattern or "?" in pattern or "[" in pattern) and fnmatch.fnmatchcase(
            field_path, pattern
        ):
            return codec
    return None


def flatten_record(record: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested mappings and lists into dotted leaf paths.

    Empty dicts ``{}`` and empty lists ``[]`` are preserved as leaves at their own
    path so structural presence is never lost.
    """
    out: dict[str, Any] = {}

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            if not node:
                if path:
                    out[path] = {}
                return
            for key in sorted(node.keys(), key=str):
                key_str = str(key)
                child_path = f"{path}.{key_str}" if path else key_str
                _walk(node[key], child_path)
        elif isinstance(node, list):
            if not node:
                if path:
                    out[path] = []
                return
            for idx, item in enumerate(node):
                child_path = f"{path}.{idx}" if path else str(idx)
                _walk(item, child_path)
        else:
            if path:
                out[path] = node

    _walk(record, prefix)
    return out


def _resolve_dotted_path(record: Optional[Mapping[str, Any]], path: str) -> Any:
    """Resolve a dotted path (supporting list indices) inside ``record``, or ``_MISSING``."""
    if record is None or not path:
        return _MISSING
    current: Any = record
    for part in path.split("."):
        if isinstance(current, Mapping):
            if part in current:
                current = current[part]
            else:
                return _MISSING
        elif isinstance(current, list):
            if part.isdigit():
                idx = int(part)
                if 0 <= idx < len(current):
                    current = current[idx]
                else:
                    return _MISSING
            else:
                return _MISSING
        else:
            return _MISSING
    return current


def _normalize_intended_delta(
    intended_delta: Mapping[str, Any],
    field_codecs: Optional[Mapping[str, FieldCodec]] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Normalize ``intended_delta`` into ``(leaf_targets, container_targets)``.

    Nested non-empty dicts and lists expand into dotted leaf targets unless an
    explicit codec is registered directly on that container path. ``container_targets``
    retains intermediate structured objects so ``EntailmentOracle`` can verify
    server-populated metadata keys on newly added structured sub-objects.
    """
    leaves: dict[str, Any] = {}
    containers: dict[str, Any] = {}

    def _expand(node: Any, path: str) -> None:
        has_direct_codec = bool(field_codecs and path in field_codecs)
        if isinstance(node, Mapping) and node and not has_direct_codec:
            containers[path] = node
            for sub_key in sorted(node.keys(), key=str):
                sub_str = str(sub_key)
                child = f"{path}.{sub_str}" if path else sub_str
                _expand(node[sub_key], child)
        elif isinstance(node, list) and node and not has_direct_codec:
            containers[path] = node
            for idx, item in enumerate(node):
                child = f"{path}.{idx}" if path else str(idx)
                _expand(item, child)
        else:
            leaves[path] = node

    for raw_key in sorted(intended_delta.keys(), key=str):
        key_str = str(raw_key)
        _expand(intended_delta[raw_key], key_str)

    return leaves, containers


def _parse_iso_date(value: str) -> Optional[_ParsedIsoDate]:
    """Parse an ISO-8601 calendar date or timestamp safely across Python 3.10+."""
    match = _ISO_DATE_RE.match(value)
    if not match:
        return None
    year_s, month_s, day_s, hour_s, min_s, sec_s, frac_s, tz_s = match.groups()
    try:
        cal_date = datetime.date(int(year_s), int(month_s), int(day_s))
    except ValueError:
        return None

    if hour_s is None:
        return _ParsedIsoDate(date=cal_date, has_time=False)

    hour = int(hour_s)
    minute = int(min_s)
    second = int(sec_s) if sec_s is not None else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return None

    microsecond = 0
    if frac_s is not None:
        padded = (frac_s[:6]).ljust(6, "0")
        microsecond = int(padded)

    tz_offset: Optional[datetime.timedelta] = None
    if tz_s is not None:
        if tz_s in ("Z", "z"):
            tz_offset = datetime.timedelta(0)
        else:
            sign = 1 if tz_s[0] == "+" else -1
            tz_digits = tz_s[1:].replace(":", "")
            tz_hours = int(tz_digits[:2])
            tz_mins = int(tz_digits[2:4])
            if tz_hours > 23 or tz_mins > 59:
                return None
            tz_offset = datetime.timedelta(minutes=sign * (tz_hours * 60 + tz_mins))

    return _ParsedIsoDate(
        date=cal_date,
        has_time=True,
        hour=hour,
        minute=minute,
        second=second,
        microsecond=microsecond,
        tz_offset=tz_offset,
    )


def canonicalize_iso_date(value: str) -> Optional[str]:
    """Return ``YYYY-MM-DD`` for bare or midnight ISO-8601 strings, or UTC ISO timestamp."""
    parsed = _parse_iso_date(value)
    if parsed is None:
        return None
    if not parsed.has_time or parsed.is_midnight:
        return parsed.date.isoformat()
    utc_dt = parsed.as_utc_datetime()
    if utc_dt is not None:
        if (
            utc_dt.hour == 0
            and utc_dt.minute == 0
            and utc_dt.second == 0
            and utc_dt.microsecond == 0
        ):
            return utc_dt.date().isoformat()
        return utc_dt.isoformat()
    return (
        f"{parsed.date.isoformat()}T"
        f"{parsed.hour:02d}:{parsed.minute:02d}:{parsed.second:02d}"
        f".{parsed.microsecond:06d}"
    )


def iso_dates_match(intended: str, stored: str) -> bool:
    """Compare two ISO-8601 date/timestamp strings safely without day drift."""
    p_int = _parse_iso_date(intended)
    p_sto = _parse_iso_date(stored)
    if p_int is None or p_sto is None:
        return False
    if not p_int.has_time and not p_sto.has_time:
        return p_int.date == p_sto.date
    if not p_int.has_time and p_sto.has_time:
        return p_sto.is_midnight and p_int.date == p_sto.date
    if p_int.has_time and not p_sto.has_time:
        return p_int.is_midnight and p_int.date == p_sto.date

    # Both include explicit time components.
    if p_int.is_midnight and p_sto.is_midnight and p_int.date == p_sto.date:
        return True
    utc_int = p_int.as_utc_datetime()
    utc_sto = p_sto.as_utc_datetime()
    if utc_int is not None and utc_sto is not None:
        return utc_int == utc_sto
    if p_int.tz_offset is None and p_sto.tz_offset is None:
        return (
            p_int.date == p_sto.date
            and p_int.hour == p_sto.hour
            and p_int.minute == p_sto.minute
            and p_int.second == p_sto.second
            and p_int.microsecond == p_sto.microsecond
        )
    return False


def canonicalize_phone(value: str) -> Optional[tuple[bool, str]]:
    """Parse a phone string into ``(has_explicit_plus, digits)``, or ``None``.

    Rejects strings containing letters, ISO-8601 dates, invalid placement of ``+``,
    or digit counts outside the 7..15 telephone range.
    """
    if not isinstance(value, str):
        return None
    if any(ch.isalpha() for ch in value):
        return None
    if _parse_iso_date(value) is not None:
        return None
    if not _PHONE_CHARS_RE.match(value):
        return None
    stripped = value.strip()
    has_plus = stripped.startswith("+")
    digits = "".join(ch for ch in stripped if ch.isdigit())
    if not (7 <= len(digits) <= 15):
        return None
    return has_plus, digits


def canonical_phones_match(intended: str, stored: str) -> bool:
    """Compare two phone strings while enforcing country-code and digit invariants."""
    parsed_int = canonicalize_phone(intended)
    parsed_sto = canonicalize_phone(stored)
    if parsed_int is None or parsed_sto is None:
        return False

    plus_int, digits_int = parsed_int
    plus_sto, digits_sto = parsed_sto

    if plus_int and plus_sto:
        return digits_int == digits_sto

    if not plus_int and not plus_sto:
        if digits_int == digits_sto:
            return True
        if (
            len(digits_int) in (8, 11)
            and digits_int.startswith("1")
            and digits_int[1:] == digits_sto
        ):
            return True
        if (
            len(digits_sto) in (8, 11)
            and digits_sto.startswith("1")
            and digits_sto[1:] == digits_int
        ):
            return True
        return False

    # Exactly one string carries an explicit '+' country prefix.
    plus_digits = digits_int if plus_int else digits_sto
    bare_digits = digits_sto if plus_int else digits_int
    if plus_digits == bare_digits:
        return True
    if (
        len(plus_digits) in (8, 11)
        and plus_digits.startswith("1")
        and plus_digits[1:] == bare_digits
    ):
        return True
    return False


def _capitalize_words_space(value: str) -> str:
    return " ".join(part.capitalize() for part in value.split(" "))


def _normalize_whitespace_case(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    return " ".join(normalized.split()).casefold()


def _has_negation_flip(intended: str, stored: str) -> bool:
    int_tokens = set(re.findall(r"[a-z]+", intended.casefold()))
    sto_tokens = set(re.findall(r"[a-z]+", stored.casefold()))
    return bool((int_tokens & _NEGATION_WORDS) ^ (sto_tokens & _NEGATION_WORDS))


def violates_hard_safety_invariant(
    intended_val: Any,
    stored_val: Any,
    codec: Optional[FieldCodec] = None,
) -> tuple[bool, str]:
    """Enforce the hard numeric, date, bool/int, and confusable-ID safety guardrail.

    Returns ``(violated, reason)``. Even a permissive custom ``EntailmentOracle``
    cannot override a violation returned by this guardrail.
    """
    if strict_equal(intended_val, stored_val):
        return False, ""

    # Never equate boolean with non-boolean (e.g. True vs 1, False vs 0).
    if isinstance(intended_val, bool) or isinstance(stored_val, bool):
        return (
            True,
            f"hard safety guardrail: boolean/type mismatch ({intended_val!r} vs {stored_val!r})",
        )

    # Never equate distinct numeric scalars.
    if isinstance(intended_val, (int, float)) and isinstance(stored_val, (int, float)):
        return (
            True,
            f"hard safety guardrail: distinct numbers ({intended_val!r} != {stored_val!r})",
        )

    if isinstance(intended_val, (int, float)) != isinstance(stored_val, (int, float)):
        return (
            True,
            f"hard safety guardrail: numeric/non-numeric type mismatch "
            f"({intended_val!r} vs {stored_val!r})",
        )

    # Recursive check on structured containers.
    if isinstance(intended_val, Mapping) and isinstance(stored_val, Mapping):
        for key in intended_val:
            if key in stored_val:
                violated, reason = violates_hard_safety_invariant(
                    intended_val[key], stored_val[key], codec=None
                )
                if violated:
                    return True, f"{key}: {reason}"
        return False, ""

    if isinstance(intended_val, list) and isinstance(stored_val, list):
        if len(intended_val) != len(stored_val):
            return (
                True,
                f"hard safety guardrail: list length mismatch ({len(intended_val)} != {len(stored_val)})",
            )
        for idx, (left_item, right_item) in enumerate(zip(intended_val, stored_val)):
            violated, reason = violates_hard_safety_invariant(
                left_item, right_item, codec=None
            )
            if violated:
                return True, f"[{idx}]: {reason}"
        return False, ""

    if not (isinstance(intended_val, str) and isinstance(stored_val, str)):
        return False, ""

    # Explicit declarative bijection table is checked in Tier 1 directly.
    if codec is not None and codec.kind == "bijection":
        return False, ""

    # ISO-8601 date check: allow identical calendar day at midnight, block distinct dates/times.
    p_int = _parse_iso_date(intended_val)
    p_sto = _parse_iso_date(stored_val)
    if p_int is not None and p_sto is not None:
        if iso_dates_match(intended_val, stored_val):
            return False, ""
        return (
            True,
            f"hard safety guardrail: ISO date/time mismatch ({intended_val!r} vs {stored_val!r})",
        )

    # Canonical phone check: allow formatting differences when canonical digits/country match.
    if canonical_phones_match(intended_val, stored_val):
        return False, ""

    int_nums = _NUMERIC_TOKEN_RE.findall(intended_val)
    sto_nums = _NUMERIC_TOKEN_RE.findall(stored_val)

    # Confusable single-token alphanumeric IDs (e.g. "AB01" vs "ABO1", "ID-1l" vs "ID-1I").
    int_stripped = intended_val.strip()
    sto_stripped = stored_val.strip()
    if (
        _IDENTIFIER_TOKEN_RE.match(int_stripped)
        and _IDENTIFIER_TOKEN_RE.match(sto_stripped)
        and (bool(int_nums) or bool(sto_nums))
        and int_stripped.casefold() != sto_stripped.casefold()
    ):
        return (
            True,
            f"hard safety guardrail: confusable alphanumeric identifier mismatch "
            f"({intended_val!r} vs {stored_val!r})",
        )

    # Distinct numeric / dosage tokens (e.g. "10 mg" vs "100 mg", "555-0188" vs "555-0189").
    if (int_nums or sto_nums) and int_nums != sto_nums:
        return (
            True,
            f"hard safety guardrail: numeric token mismatch ({intended_val!r} vs {stored_val!r})",
        )

    return False, ""


def apply_field_codec(
    codec: FieldCodec,
    intended_val: Any,
    stored_val: Any,
) -> tuple[bool, str]:
    """Evaluate Tier-1 ``FieldCodec`` normalization or bijection between two values."""
    kind = codec.kind
    if kind == "exact":
        ok = strict_equal(intended_val, stored_val)
        return (
            ok,
            "exact codec match"
            if ok
            else f"exact mismatch ({intended_val!r} != {stored_val!r})",
        )

    if not (isinstance(intended_val, str) and isinstance(stored_val, str)):
        return (
            False,
            f"codec {kind!r} requires string values, got "
            f"{type(intended_val).__name__} and {type(stored_val).__name__}",
        )

    if kind == "bijection":
        if intended_val in codec.mapping:
            mapped = codec.mapping[intended_val]
            if mapped == stored_val or mapped.casefold() == stored_val.casefold():
                return True, f"bijection mapped {intended_val!r} -> {stored_val!r}"
        int_norm = intended_val.strip().casefold()
        for map_key, map_val in codec.mapping.items():
            if map_key.strip().casefold() == int_norm:
                if (
                    map_val == stored_val
                    or map_val.strip().casefold() == stored_val.strip().casefold()
                ):
                    return (
                        True,
                        f"bijection (case-insensitive) mapped {intended_val!r} -> {stored_val!r}",
                    )
        # Support inverse direction when codec.mapping is a true 1-to-1 bijection.
        if codec.mapping and len(set(codec.mapping.values())) == len(codec.mapping):
            for map_key, map_val in codec.mapping.items():
                if map_val == intended_val and (
                    map_key == stored_val or map_key.casefold() == stored_val.casefold()
                ):
                    return (
                        True,
                        f"inverse bijection mapped {intended_val!r} -> {stored_val!r}",
                    )
        return False, f"bijection lookup failed for {intended_val!r} -> {stored_val!r}"

    violated, violation_reason = violates_hard_safety_invariant(
        intended_val, stored_val, codec=codec
    )
    if violated:
        return False, violation_reason

    if kind == "casefold":
        ok = intended_val.casefold() == stored_val.casefold()
        return (
            ok,
            "casefold match"
            if ok
            else f"casefold mismatch ({intended_val!r} vs {stored_val!r})",
        )

    if kind == "upper":
        ok = stored_val == intended_val.upper()
        return (
            ok,
            "upper codec match"
            if ok
            else f"upper mismatch ({stored_val!r} != {intended_val.upper()!r})",
        )

    if kind == "lower":
        ok = stored_val == intended_val.lower()
        return (
            ok,
            "lower codec match"
            if ok
            else f"lower mismatch ({stored_val!r} != {intended_val.lower()!r})",
        )

    if kind == "capitalize_words":
        ok = (
            stored_val == _capitalize_words_space(intended_val)
            or stored_val == intended_val.title()
        )
        return (
            ok,
            "capitalize_words codec match"
            if ok
            else f"capitalize_words mismatch ({intended_val!r} vs {stored_val!r})",
        )

    if kind == "strip_whitespace":
        ok = " ".join(intended_val.split()) == " ".join(stored_val.split())
        return (
            ok,
            "strip_whitespace codec match"
            if ok
            else f"strip_whitespace mismatch ({intended_val!r} vs {stored_val!r})",
        )

    if kind == "phone_digits":
        ok = canonical_phones_match(intended_val, stored_val)
        return (
            ok,
            "phone_digits codec match"
            if ok
            else f"phone_digits mismatch ({intended_val!r} vs {stored_val!r})",
        )

    if kind == "iso_date":
        ok = iso_dates_match(intended_val, stored_val)
        return (
            ok,
            "iso_date codec match"
            if ok
            else f"iso_date mismatch ({intended_val!r} vs {stored_val!r})",
        )

    return False, f"unsupported codec kind: {kind!r}"


class SafeNormalizedEntailmentOracle(EntailmentOracle):
    """Deterministic Tier-2 entailment oracle for safe canonical & subset-object transforms.

    Handles:
    - Case and whitespace canonicalization (rejecting negations, digit edits, or distinct words)
    - ISO-8601 bare date vs midnight timestamp (``YYYY-MM-DD`` == ``YYYY-MM-DDT00:00:00Z``)
    - E.164 / NANP telephone formatting canonicalization
    - Subset structured objects (e.g. ``telecom`` entries where the server adds benign
      metadata keys such as ``"rank": 1`` without contradicting intended keys)
    """

    def __init__(
        self,
        *,
        allowed_metadata_keys: frozenset[str] = DEFAULT_ORACLE_METADATA_KEYS,
    ) -> None:
        self._allowed_metadata_keys = allowed_metadata_keys

    @property
    def allowed_metadata_keys(self) -> frozenset[str]:
        return self._allowed_metadata_keys

    def entails(
        self,
        field_path: str,
        intended_value: Any,
        stored_value: Any,
        before_value: Any = None,
    ) -> tuple[bool, str]:
        if strict_equal(intended_value, stored_value):
            return True, "exact match"

        violated, violation_reason = violates_hard_safety_invariant(
            intended_value, stored_value, codec=None
        )
        if violated:
            return False, violation_reason

        if isinstance(intended_value, str) and isinstance(stored_value, str):
            if _has_negation_flip(intended_value, stored_value):
                return (
                    False,
                    f"negation token mismatch on {field_path!r}: "
                    f"{intended_value!r} vs {stored_value!r}",
                )
            if iso_dates_match(intended_value, stored_value):
                return True, "ISO-8601 midnight date canonical entailment"
            if canonical_phones_match(intended_value, stored_value):
                return True, "E.164/NANP phone canonical entailment"
            if iso_subdivisions_match(intended_value, stored_value):
                return True, "ISO-3166-2 subdivision postal code entailment"
            if _normalize_whitespace_case(intended_value) == _normalize_whitespace_case(
                stored_value
            ):
                return True, "case/whitespace canonical entailment"
            return (
                False,
                f"distinct token or representation on {field_path!r}: "
                f"{intended_value!r} vs {stored_value!r}",
            )

        if isinstance(intended_value, Mapping) and isinstance(stored_value, Mapping):
            before_map = before_value if isinstance(before_value, Mapping) else None
            for key, int_sub in intended_value.items():
                if key not in stored_value:
                    return False, f"missing intended key {key!r} under {field_path!r}"
                before_sub = before_map.get(key) if before_map is not None else None
                sub_ok, sub_reason = self.entails(
                    f"{field_path}.{key}" if field_path else str(key),
                    int_sub,
                    stored_value[key],
                    before_sub,
                )
                if not sub_ok:
                    return False, sub_reason

            extra_keys = sorted(
                (str(k) for k in stored_value.keys() if k not in intended_value),
                key=str,
            )
            for extra_key in extra_keys:
                extra_val = stored_value[extra_key]
                if before_map is not None and extra_key in before_map:
                    if not strict_equal(
                        extra_val, before_map[extra_key]
                    ) and extra_key not in {
                        "version",
                        "versionId",
                        "etag",
                        "updated_at",
                        "updatedAt",
                        "lastUpdated",
                        "meta",
                    }:
                        return (
                            False,
                            f"existing sibling key {extra_key!r} under {field_path!r} mutated",
                        )
                    continue
                if _is_empty_server_default_value(extra_val):
                    continue
                if extra_key in self._allowed_metadata_keys:
                    continue
                return (
                    False,
                    f"unexpected non-metadata key {extra_key!r} in structured object {field_path!r}",
                )

            return (
                True,
                f"subset structured object entailed (metadata keys: {extra_keys})"
                if extra_keys
                else "structured object entailed",
            )

        if isinstance(intended_value, list) and isinstance(stored_value, list):
            if len(intended_value) != len(stored_value):
                return (
                    False,
                    f"list length mismatch on {field_path!r}: "
                    f"{len(intended_value)} != {len(stored_value)}",
                )
            before_list = before_value if isinstance(before_value, list) else None
            for idx, (int_item, sto_item) in enumerate(
                zip(intended_value, stored_value)
            ):
                before_item = (
                    before_list[idx]
                    if before_list is not None and idx < len(before_list)
                    else None
                )
                item_ok, item_reason = self.entails(
                    f"{field_path}.{idx}" if field_path else str(idx),
                    int_item,
                    sto_item,
                    before_item,
                )
                if not item_ok:
                    return False, item_reason
            return True, "structured list entailed"

        return (
            False,
            f"non-entailed values on {field_path!r}: {intended_value!r} vs {stored_value!r}",
        )


_ISO_SUBDIVISION_MAP: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB",
    "new brunswick": "NB", "newfoundland and labrador": "NL",
    "northwest territories": "NT", "nova scotia": "NS", "nunavut": "NU",
    "ontario": "ON", "prince edward island": "PE", "quebec": "QC",
    "saskatchewan": "SK", "yukon": "YT",
}


def iso_subdivisions_match(intended: str, stored: str) -> bool:
    """Exact 1-to-1 ISO-3166-2 US/CA subdivision full-name <-> 2-letter code check."""
    a = _normalize_whitespace_case(intended)
    b = _normalize_whitespace_case(stored)
    if not a or not b or a == b:
        return False
    code_a = _ISO_SUBDIVISION_MAP.get(a)
    if code_a is not None and code_a.lower() == b:
        return True
    code_b = _ISO_SUBDIVISION_MAP.get(b)
    if code_b is not None and code_b.lower() == a:
        return True
    return False


StructuralEntailmentOracle = SafeNormalizedEntailmentOracle


class FrameAndIntentJudge(JudgeProtocol):
    """Pierce Bidirectional Lens Judge enforcing Frame Preservation + Intent Entailment."""

    def __init__(
        self,
        *,
        entailment_oracle: Optional[EntailmentOracle] = None,
        allowed_server_defaults: tuple[str, ...] = DEFAULT_ALLOWED_SERVER_DEFAULTS,
        bijection_cache: Optional[BijectionCache] = None,
        cache_salt: Optional[bytes] = None,
    ) -> None:
        self._entailment_oracle = entailment_oracle
        self._allowed_server_defaults = tuple(allowed_server_defaults)
        self._bijection_cache = bijection_cache
        self._cache_salt = (
            cache_salt if cache_salt is not None else secrets.token_bytes(32)
        )

    @property
    def entailment_oracle(self) -> Optional[EntailmentOracle]:
        return self._entailment_oracle

    @property
    def allowed_server_defaults(self) -> tuple[str, ...]:
        return self._allowed_server_defaults

    @property
    def bijection_cache(self) -> Optional[BijectionCache]:
        return self._bijection_cache

    @property
    def cache_salt(self) -> bytes:
        return self._cache_salt

    def compute_pair_digest(self, intended_value: Any, stored_value: Any) -> str:
        """Compute salted HMAC-SHA256 digest for ``(intended_value, stored_value)``."""
        return salted_sha256(
            self._cache_salt,
            {"intended": intended_value, "stored": stored_value},
        )

    def _cache_contains(self, key: tuple[str, str]) -> bool:
        if self._bijection_cache is None:
            return False
        if isinstance(self._bijection_cache, MutableMapping):
            return bool(self._bijection_cache.get(key))
        return key in self._bijection_cache

    def _cache_store(self, key: tuple[str, str], reason: str) -> None:
        if self._bijection_cache is None:
            return
        if isinstance(self._bijection_cache, MutableMapping):
            self._bijection_cache[key] = reason
        else:
            self._bijection_cache.add(key)

    def _has_referenced_descendant(
        self,
        path: str,
        v0: Any,
        v1: Any,
        intended_leaves: Mapping[str, Any],
        volatile_fields: Sequence[str],
        field_codecs: Optional[Mapping[str, FieldCodec]],
    ) -> bool:
        prefix = f"{path}."
        if any(k.startswith(prefix) for k in intended_leaves):
            return True
        if field_codecs and any(k.startswith(prefix) for k in field_codecs):
            return True

        candidate_leaves: set[str] = set()
        if isinstance(v0, Mapping) and v0:
            candidate_leaves.update(flatten_record(v0, prefix=path).keys())
        elif isinstance(v0, list) and v0:
            r0_list_leaves = flatten_record({"_": v0}).keys()
            candidate_leaves.update(
                f"{path}{k[1:]}" if k.startswith("_.") else k for k in r0_list_leaves
            )

        if isinstance(v1, Mapping) and v1:
            candidate_leaves.update(flatten_record(v1, prefix=path).keys())
        elif isinstance(v1, list) and v1:
            r1_list_leaves = flatten_record({"_": v1}).keys()
            candidate_leaves.update(
                f"{path}{k[1:]}" if k.startswith("_.") else k for k in r1_list_leaves
            )

        for leaf in candidate_leaves:
            if leaf.startswith(prefix):
                if matches_field_pattern(leaf, volatile_fields):
                    return True
                if _lookup_field_codec(leaf, field_codecs) is not None:
                    return True
        return False

    def _should_descend(
        self,
        path: str,
        v0: Any,
        v1: Any,
        r0_is_none: bool,
        intended_leaves: Mapping[str, Any],
        volatile_fields: Sequence[str],
        field_codecs: Optional[Mapping[str, FieldCodec]],
    ) -> Optional[str]:
        """Return ``'mapping'``, ``'list'``, or ``None`` if ``path`` should be evaluated directly."""
        if not path:
            return "mapping"

        # If path itself is an explicit intended target or has an exact codec, evaluate at path.
        if path in intended_leaves:
            return None
        if field_codecs and path in field_codecs:
            return None
        if matches_field_pattern(path, volatile_fields):
            return None

        v0_nonempty_map = isinstance(v0, Mapping) and len(v0) > 0
        v1_nonempty_map = isinstance(v1, Mapping) and len(v1) > 0
        v0_nonempty_list = isinstance(v0, list) and len(v0) > 0
        v1_nonempty_list = isinstance(v1, list) and len(v1) > 0

        if v0_nonempty_map and v1_nonempty_map:
            return "mapping"
        if v0_nonempty_list and v1_nonempty_list:
            return "list"

        # On create (r0 is None), expand r1's structure into leaves.
        if r0_is_none:
            if v1_nonempty_map:
                return "mapping"
            if v1_nonempty_list:
                return "list"
            return None

        # On update, descend into partial/mismatched containers only when a descendant
        # is explicitly intended, volatile, or codec-governed.
        if self._has_referenced_descendant(
            path, v0, v1, intended_leaves, volatile_fields, field_codecs
        ):
            if v0_nonempty_map or v1_nonempty_map:
                return "mapping"
            if v0_nonempty_list or v1_nonempty_list:
                return "list"

        return None

    def _evaluate_intended_field(
        self,
        field_path: str,
        intended_val: Any,
        r1_val: Any,
        r0_val: Any,
        field_codecs: Optional[Mapping[str, FieldCodec]],
    ) -> FieldVerdict:
        if r1_val is _MISSING:
            return FieldVerdict(
                field_path=field_path,
                category="intended",
                passed=False,
                tier_used="failed",
                reason=f"intended field {field_path!r} is missing from r1",
            )

        # Tier 0: Exact value & type equality.
        if strict_equal(r1_val, intended_val):
            return FieldVerdict(
                field_path=field_path,
                category="intended",
                passed=True,
                tier_used="exact",
                reason="exact match",
            )

        codec = _lookup_field_codec(field_path, field_codecs)

        # Safety Guardrail First: hard numeric / date / identifier / bool-vs-int check.
        violated, violation_reason = violates_hard_safety_invariant(
            intended_val, r1_val, codec=codec
        )
        if violated:
            return FieldVerdict(
                field_path=field_path,
                category="intended",
                passed=False,
                tier_used="failed",
                reason=violation_reason,
            )

        # Tier 1a: Explicit declarative FieldCodec.
        if codec is not None and codec.kind != "exact":
            codec_ok, codec_reason = apply_field_codec(codec, intended_val, r1_val)
            if codec_ok:
                return FieldVerdict(
                    field_path=field_path,
                    category="intended",
                    passed=True,
                    tier_used="codec",
                    reason=codec_reason,
                )
            if self._entailment_oracle is None:
                return FieldVerdict(
                    field_path=field_path,
                    category="intended",
                    passed=False,
                    tier_used="failed",
                    reason=codec_reason,
                )

        # Tier 1b: Salted bijection_cache hit (when codec did not explicitly forbid via exact-only).
        if self._bijection_cache is not None and (
            codec is None or codec.kind != "exact"
        ):
            digest = self.compute_pair_digest(intended_val, r1_val)
            cache_key = (field_path, digest)
            if self._cache_contains(cache_key):
                return FieldVerdict(
                    field_path=field_path,
                    category="intended",
                    passed=True,
                    tier_used="codec",
                    reason="verified via salted bijection_cache",
                )

        # Tier 2: Opt-in EntailmentOracle (only when codec is not explicitly pinned to "exact").
        if self._entailment_oracle is not None and (
            codec is None or codec.kind != "exact"
        ):
            before_arg = None if r0_val is _MISSING else r0_val
            entailed, oracle_reason = self._entailment_oracle.entails(
                field_path, intended_val, r1_val, before_arg
            )
            if entailed:
                if self._bijection_cache is not None:
                    digest = self.compute_pair_digest(intended_val, r1_val)
                    self._cache_store((field_path, digest), oracle_reason)
                return FieldVerdict(
                    field_path=field_path,
                    category="intended",
                    passed=True,
                    tier_used="entailment_oracle",
                    reason=oracle_reason,
                )
            return FieldVerdict(
                field_path=field_path,
                category="intended",
                passed=False,
                tier_used="failed",
                reason=oracle_reason,
            )

        return FieldVerdict(
            field_path=field_path,
            category="intended",
            passed=False,
            tier_used="failed",
            reason=f"intended value {intended_val!r} != stored value {r1_val!r}",
        )

    def _entailed_by_intended_container(
        self,
        field_path: str,
        r0: Optional[Mapping[str, Any]],
        r1: Mapping[str, Any],
        intended_containers: Mapping[str, Any],
    ) -> Optional[str]:
        """Check if a newly server-populated sibling key belongs to an oracle-entailed container."""
        if self._entailment_oracle is None or not intended_containers:
            return None

        parts = field_path.split(".")
        # Check from most specific ancestor container up to top-level container.
        for depth in range(len(parts) - 1, 0, -1):
            ancestor = ".".join(parts[:depth])
            if ancestor not in intended_containers:
                continue
            intended_sub = intended_containers[ancestor]
            r1_sub = _resolve_dotted_path(r1, ancestor)
            if r1_sub is _MISSING:
                continue
            r0_sub = _resolve_dotted_path(r0, ancestor)
            before_sub = None if r0_sub is _MISSING else r0_sub
            violated, _ = violates_hard_safety_invariant(
                intended_sub, r1_sub, codec=None
            )
            if violated:
                return None
            ok, reason = self._entailment_oracle.entails(
                ancestor, intended_sub, r1_sub, before_sub
            )
            if ok:
                return reason
        return None

    def evaluate(
        self,
        r0: Optional[Mapping[str, Any]],
        r1: Mapping[str, Any],
        intended_delta: Mapping[str, Any],
        *,
        volatile_fields: tuple[str, ...] = (),
        field_codecs: Optional[Mapping[str, FieldCodec]] = None,
        allowed_server_defaults: Optional[tuple[str, ...]] = None,
    ) -> JudgeVerdict:
        effective_defaults = (
            tuple(dict.fromkeys(self._allowed_server_defaults + tuple(allowed_server_defaults)))
            if allowed_server_defaults is not None
            else self._allowed_server_defaults
        )
        intended_leaves, intended_containers = _normalize_intended_delta(
            intended_delta, field_codecs=field_codecs
        )

        verdicts: list[FieldVerdict] = []
        clobbered: list[str] = []
        mismatched: list[str] = []
        extra: list[str] = []
        reasons: list[str] = []
        visited_intended: set[str] = set()

        r0_is_none = r0 is None

        def _visit(path: str, v0: Any, v1: Any) -> None:
            descend_kind = self._should_descend(
                path,
                v0,
                v1,
                r0_is_none,
                intended_leaves,
                volatile_fields,
                field_codecs,
            )
            if descend_kind == "mapping":
                keys_set: set[str] = set()
                if isinstance(v0, Mapping):
                    keys_set.update(str(k) for k in v0.keys())
                if isinstance(v1, Mapping):
                    keys_set.update(str(k) for k in v1.keys())
                for key_str in sorted(keys_set):
                    child_path = f"{path}.{key_str}" if path else key_str
                    c0 = (
                        v0[key_str]
                        if isinstance(v0, Mapping) and key_str in v0
                        else _MISSING
                    )
                    c1 = (
                        v1[key_str]
                        if isinstance(v1, Mapping) and key_str in v1
                        else _MISSING
                    )
                    _visit(child_path, c0, c1)
                return

            if descend_kind == "list":
                len0 = len(v0) if isinstance(v0, list) else 0
                len1 = len(v1) if isinstance(v1, list) else 0
                for idx in range(max(len0, len1)):
                    child_path = f"{path}.{idx}" if path else str(idx)
                    c0 = v0[idx] if isinstance(v0, list) and idx < len0 else _MISSING
                    c1 = v1[idx] if isinstance(v1, list) and idx < len1 else _MISSING
                    _visit(child_path, c0, c1)
                return

            if not path:
                return

            # 1. Intended-field verification (Law 2) always takes precedence over volatile_fields.
            if path in intended_leaves:
                visited_intended.add(path)
                fv = self._evaluate_intended_field(
                    path, intended_leaves[path], v1, v0, field_codecs
                )
                verdicts.append(fv)
                if not fv.passed:
                    mismatched.append(path)
                    reasons.append(f"intent mismatch on {path}: {fv.reason}")
                return

            # 2. Volatile fields are explicitly excluded from frame comparison.
            if matches_field_pattern(path, volatile_fields):
                verdicts.append(
                    FieldVerdict(
                        field_path=path,
                        category="volatile",
                        passed=True,
                        tier_used="volatile_skip",
                        reason="matched volatile_fields",
                    )
                )
                return

            # 3. Law 1 on Create (r0 is None).
            if r0_is_none:
                if _is_empty_server_default_value(v1) or matches_field_pattern(
                    path, effective_defaults
                ):
                    verdicts.append(
                        FieldVerdict(
                            field_path=path,
                            category="frame",
                            passed=True,
                            tier_used="frame_eq",
                            reason="allowed server default on create",
                        )
                    )
                    return

                container_reason = self._entailed_by_intended_container(
                    path, r0, r1, intended_containers
                )
                if container_reason is not None:
                    verdicts.append(
                        FieldVerdict(
                            field_path=path,
                            category="intended",
                            passed=True,
                            tier_used="entailment_oracle",
                            reason=container_reason,
                        )
                    )
                    return

                extra_reason = f"unexpected populated field on create: {path}={v1!r}"
                verdicts.append(
                    FieldVerdict(
                        field_path=path,
                        category="frame",
                        passed=False,
                        tier_used="failed",
                        reason=extra_reason,
                    )
                )
                extra.append(path)
                reasons.append(extra_reason)
                return

            # 4. Law 1 on Update (r0 is not None): strict frame preservation.
            if v0 is not _MISSING and v1 is not _MISSING and strict_equal(v1, v0):
                verdicts.append(
                    FieldVerdict(
                        field_path=path,
                        category="frame",
                        passed=True,
                        tier_used="frame_eq",
                        reason="unchanged from r0",
                    )
                )
                return

            if v0 is _MISSING and v1 is not _MISSING:
                container_reason = self._entailed_by_intended_container(
                    path, r0, r1, intended_containers
                )
                if container_reason is not None:
                    verdicts.append(
                        FieldVerdict(
                            field_path=path,
                            category="intended",
                            passed=True,
                            tier_used="entailment_oracle",
                            reason=container_reason,
                        )
                    )
                    return
                inject_reason = f"unexpected extra field added in r1: {path}={v1!r}"
                verdicts.append(
                    FieldVerdict(
                        field_path=path,
                        category="frame",
                        passed=False,
                        tier_used="failed",
                        reason=inject_reason,
                    )
                )
                extra.append(path)
                reasons.append(inject_reason)
                return

            if v1 is _MISSING:
                wipe_reason = f"untouched field {path!r} deleted in r1 (was {v0!r})"
            else:
                wipe_reason = (
                    f"untouched field {path!r} clobbered from {v0!r} to {v1!r}"
                )
            verdicts.append(
                FieldVerdict(
                    field_path=path,
                    category="frame",
                    passed=False,
                    tier_used="failed",
                    reason=wipe_reason,
                )
            )
            clobbered.append(path)
            reasons.append(wipe_reason)

        _visit("", r0 if r0 is not None else _MISSING, r1)

        # Ensure any intended leaf absent from both r0 and r1 is still evaluated.
        for intended_path in sorted(intended_leaves.keys()):
            if intended_path not in visited_intended:
                resolved_r1 = _resolve_dotted_path(r1, intended_path)
                resolved_r0 = _resolve_dotted_path(r0, intended_path)
                fv = self._evaluate_intended_field(
                    intended_path,
                    intended_leaves[intended_path],
                    resolved_r1,
                    resolved_r0,
                    field_codecs,
                )
                verdicts.append(fv)
                if not fv.passed:
                    mismatched.append(intended_path)
                    reasons.append(f"intent mismatch on {intended_path}: {fv.reason}")

        frame_preserved = len(clobbered) == 0 and len(extra) == 0
        intent_satisfied = len(mismatched) == 0
        passed = frame_preserved and intent_satisfied

        return JudgeVerdict(
            passed=passed,
            frame_preserved=frame_preserved,
            intent_satisfied=intent_satisfied,
            clobbered_fields=tuple(clobbered),
            mismatched_fields=tuple(mismatched),
            extra_fields=tuple(extra),
            field_verdicts=tuple(verdicts),
            reasons=tuple(reasons),
        )


def _is_ordered_code_abbreviation(label: str, code: str) -> bool:
    """Return True if ``code`` is a 1..4 uppercase letter abbreviation of ``label``.

    Requires ``label`` to be a non-uppercase human phrase at least 2 characters longer
    than ``code`` (minimum length 4), sharing the same leading initial letter, with all
    characters of ``code`` appearing in order within ``label``'s alphabetic characters.
    Rejects unrelated names or truncations like ``("Alice", "Bob")`` or ``("Bo", "Bob")``.
    """
    if not _UPPER_CODE_RE.match(code):
        return False
    if label.isupper() or len(label) < 4 or len(label) < len(code) + 2:
        return False
    label_letters = "".join(ch.upper() for ch in label if ch.isalpha())
    if not label_letters or label_letters[0] != code[0]:
        return False
    it = iter(label_letters)
    return all(ch in it for ch in code)


def _is_enum_slug_pair(left: str, right: str) -> bool:
    """Return True if ``left`` and ``right`` are display-label vs enum-slug equivalents."""
    if not any(sep in left or sep in right for sep in ("_", "-")):
        return False
    norm_left = re.sub(r"[\s_-]+", "", left).casefold()
    norm_right = re.sub(r"[\s_-]+", "", right).casefold()
    return len(norm_left) >= 3 and norm_left == norm_right


def infer_field_codec(intended_val: Any, stored_val: Any) -> Optional[FieldCodec]:
    """Infer a declarative ``FieldCodec`` from a sandbox ``(intended, stored)`` pair.

    Returns ``None`` on corrupt, unrelated, type-mismatched, or numerically
    conflicting pairs (e.g. ``("Alice", "Bob")``, ``("10", "20")``).
    """
    if strict_equal(intended_val, stored_val):
        return FieldCodec(kind="exact")

    if isinstance(intended_val, bool) or isinstance(stored_val, bool):
        return None
    if not (isinstance(intended_val, str) and isinstance(stored_val, str)):
        return None
    if not intended_val or not stored_val:
        return None

    violated, _ = violates_hard_safety_invariant(intended_val, stored_val, codec=None)
    if violated:
        return None

    if iso_dates_match(intended_val, stored_val):
        return FieldCodec(kind="iso_date")

    if canonical_phones_match(intended_val, stored_val):
        return FieldCodec(kind="phone_digits")

    if " ".join(intended_val.split()) == " ".join(stored_val.split()):
        return FieldCodec(kind="strip_whitespace")

    has_alpha = any(ch.isalpha() for ch in intended_val)
    if has_alpha and stored_val == intended_val.upper():
        return FieldCodec(kind="upper")

    if has_alpha and stored_val == intended_val.lower():
        return FieldCodec(kind="lower")

    if has_alpha and (
        stored_val == _capitalize_words_space(intended_val)
        or stored_val == intended_val.title()
    ):
        return FieldCodec(kind="capitalize_words")

    if has_alpha and intended_val.casefold() == stored_val.casefold():
        return FieldCodec(kind="casefold")

    if (
        _is_ordered_code_abbreviation(intended_val.strip(), stored_val.strip())
        or _is_ordered_code_abbreviation(stored_val.strip(), intended_val.strip())
        or _is_enum_slug_pair(intended_val.strip(), stored_val.strip())
    ):
        return FieldCodec(kind="bijection", mapping={intended_val: stored_val})

    return None


__all__ = [
    "BijectionCache",
    "DEFAULT_ALLOWED_SERVER_DEFAULTS",
    "DEFAULT_ORACLE_METADATA_KEYS",
    "FrameAndIntentJudge",
    "SafeNormalizedEntailmentOracle",
    "StructuralEntailmentOracle",
    "apply_field_codec",
    "canonical_phones_match",
    "canonicalize_iso_date",
    "canonicalize_phone",
    "flatten_record",
    "infer_field_codec",
    "iso_dates_match",
    "matches_field_pattern",
    "strict_equal",
    "violates_hard_safety_invariant",
]
