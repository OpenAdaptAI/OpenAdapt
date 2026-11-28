"""Deterministic typed canonicalizers and semantic slot-label compatibility verification.

Provides non-LLM post-commit PutGet value comparison across portal display formats
(dates, integer-cent currency, E.164/10-digit phone numbers, suffix-masked identifiers,
structured clinical/accounting code prefixes, and whitespace/case-normalized strings)
alongside before-commit detection of same-type slot ambiguity and role conflicts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import (
    AbstractSet,
    Any,
    Dict,
    FrozenSet,
    Iterable,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from openadapt_kernel.macro import ControlSchema

FieldType = Literal[
    "exact",
    "date",
    "currency",
    "phone",
    "masked_id",
    "code_prefix",
    "casefold_ws",
]

VALID_FIELD_TYPES: FrozenSet[str] = frozenset(
    {"exact", "date", "currency", "phone", "masked_id", "code_prefix", "casefold_ws"}
)

SlotBindingStatus = Literal["OK", "AMBIGUOUS_SLOT"]

_UNICODE_DASH_RE = re.compile(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")
_MULTI_SPACE_RE = re.compile(r"\s+")
_AGE_BADGE_RE = re.compile(
    r"\s*\((?:age[:\s]*)?\d{1,3}\s*"
    r"(?:y(?:rs?|ears?|o)?(?:\s*\d{1,2}\s*m(?:o|os|onths?)?)?|m(?:o|os|onths?))\)\s*$",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_US_DATE_RE = re.compile(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$")
_DOT_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
_NAMED_MONTH_DATE_RE = re.compile(
    r"^([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})$"
)
_DMY_NAMED_MONTH_DATE_RE = re.compile(
    r"^(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})$"
)
DateOrder = Literal["MDY", "DMY", "YMD", "STRICT_UNAMBIGUOUS"]
_CJK_RUN_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+")
_CURRENCY_CODE_SYM_RE = re.compile(
    r"(?:USD|EUR|GBP|CAD|CHF|JPY|AUD|NZD|SEK|NOK|DKK|PLN|MXN|BRL|INR|[$€£¥₹₽₩])",
    re.IGNORECASE,
)
_EU_GROUPED_NUM_RE = re.compile(r"^(?:\d{1,3}(?:[.\s\u00a0']\d{3})+|\d+)(?:,\d{1,2})?$")
_SPACE_US_GROUPED_NUM_RE = re.compile(r"^(?:\d{1,3}(?:[,\s\u00a0']\d{3})+|\d+)(?:\.\d{1,2})?$")
_MONTH_MAP: Dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_GROUPED_NUM_RE = re.compile(r"^(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?$")
_PHONE_ALLOWED_RE = re.compile(r"^[\d\s().+-]+$")
_SYMBOL_MASK_RE = re.compile(
    r"^(?P<prefix>[A-Za-z0-9]*)(?P<mask>[*\u2022\u25cf\u00b7]+)(?P<suffix>[A-Za-z0-9]+)$"
)
_X_MASK_RE = re.compile(
    r"^(?P<prefix>[A-WYZa-wyz0-9]*)(?P<mask>[Xx]{2,})(?P<suffix>[A-WYZa-wyz0-9][A-Za-z0-9]*)$"
)
_CODE_TOKEN_RE = re.compile(r"^[A-Z0-9]+(?:[._/-][A-Z0-9]+)*$")
_SPACED_PREFIX_CODE_RE = re.compile(r"^([A-Za-z]{1,6})\s*-\s*([0-9][A-Za-z0-9.]*)$")
_CODE_PLUS_PROSE_RE = re.compile(r"^([A-Za-z0-9]+(?:[._/-][A-Za-z0-9]+)*)\s+[A-Za-z]{2,}.*$")

_GENERIC_TYPE_TOKENS: FrozenSet[str] = frozenset(
    {
        "date", "dob", "birth", "birthdate", "day", "month", "year",
        "amount", "amt", "currency", "usd", "fee", "cost", "price", "value", "total",
        "phone", "tel", "telephone", "number", "num", "no",
        "id", "identifier", "ref", "reference", "code", "field", "input", "text",
        "of", "the", "for", "and", "or", "in", "to", "by",
    }
)


def _norm_dashes_ws(value: str) -> str:
    s = _UNICODE_DASH_RE.sub("-", value or "")
    return _MULTI_SPACE_RE.sub(" ", s).strip()


def normalize_casefold_ws(value: str) -> str:
    """Normalize Unicode dashes, collapse internal whitespace, strip, and casefold."""
    return _norm_dashes_ws(value).casefold()


def tokenize_role_label(text: str) -> FrozenSet[str]:
    """Split camelCase, Unicode words, and CJK runs (chars + bigrams) into casefolded tokens."""
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    tokens: set[str] = set()
    for word in re.findall(r"[^\W_]+", expanded.casefold()):
        tokens.add(word)
        for cjk_m in _CJK_RUN_RE.finditer(word):
            run = cjk_m.group(0)
            tokens.add(run)
            for ch in run:
                tokens.add(ch)
            for i in range(len(run) - 1):
                tokens.add(run[i : i + 2])
    return frozenset(tokens)


def is_ambiguous_numeric_date(value: str) -> bool:
    """Return True when a numeric DD/MM/YYYY vs MM/DD/YYYY date has two distinct values <= 12."""
    s = _AGE_BADGE_RE.sub("", _norm_dashes_ws(value)).strip()
    m = _US_DATE_RE.match(s) or _DOT_DATE_RE.match(s)
    if not m:
        return False
    a, b = int(m.group(1)), int(m.group(2))
    return 1 <= a <= 12 and 1 <= b <= 12 and a != b


def parse_canonical_date(
    value: str, *, date_order: DateOrder = "MDY"
) -> Optional[Tuple[int, int, int]]:
    """Parse ISO, MDY/DMY/STRICT_UNAMBIGUOUS numeric, DD.MM.YYYY, Mon DD YYYY, or DD Mon YYYY dates."""
    s = _AGE_BADGE_RE.sub("", _norm_dashes_ws(value)).strip()
    if not s:
        return None

    year = month = day = 0
    m_iso = _ISO_DATE_RE.match(s)
    if m_iso:
        year, month, day = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
    elif (m_num := _US_DATE_RE.match(s) or _DOT_DATE_RE.match(s)) is not None:
        a, b, year = int(m_num.group(1)), int(m_num.group(2)), int(m_num.group(3))
        is_dot = _DOT_DATE_RE.match(s) is not None
        if date_order == "STRICT_UNAMBIGUOUS":
            if 1 <= a <= 12 and 1 <= b <= 12 and a != b:
                return None
            if a > 12:
                day, month = a, b
            else:
                month, day = a, b
        elif date_order == "DMY" or (is_dot and a > 12) or is_dot:
            day, month = a, b
        else:
            month, day = a, b
    elif (m_named := _NAMED_MONTH_DATE_RE.match(s)) is not None:
        mon_key = m_named.group(1).lower()
        if mon_key not in _MONTH_MAP:
            return None
        month, day, year = _MONTH_MAP[mon_key], int(m_named.group(2)), int(m_named.group(3))
    elif (m_dmy_named := _DMY_NAMED_MONTH_DATE_RE.match(s)) is not None:
        mon_key = m_dmy_named.group(2).lower()
        if mon_key not in _MONTH_MAP:
            return None
        day, month, year = int(m_dmy_named.group(1)), _MONTH_MAP[mon_key], int(m_dmy_named.group(3))
    else:
        return None

    if year < 1000:
        return None
    try:
        valid = date(year, month, day)
    except ValueError:
        return None
    return (valid.year, valid.month, valid.day)


def parse_canonical_currency_cents(value: str) -> Optional[int]:
    """Parse US/UK, EU/LatAm (1.234,56 €), Swiss (CHF 1'234.50), and ISO currency strings into integer cents."""
    s = _norm_dashes_ws(value)
    if not s:
        return None

    negative = False
    has_parens = False
    if s.startswith("$(") and s.endswith(")"):
        negative, has_parens, s = True, True, s[2:-1].strip()
    elif s.startswith("(") and s.endswith(")"):
        negative, has_parens, s = True, True, s[1:-1].strip()

    for prefix_sign in ("-", "+"):
        if s.startswith(prefix_sign):
            if has_parens:
                return None
            if prefix_sign == "-":
                negative = True
            s = s[1:].strip()
            break

    # Strip leading/trailing ISO codes or currency symbols (e.g., EUR, CHF, €, £, $)
    s = re.sub(rf"^(?:{_CURRENCY_CODE_SYM_RE.pattern}\s*)+", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(rf"(?:\s*{_CURRENCY_CODE_SYM_RE.pattern})+$", "", s, flags=re.IGNORECASE).strip()

    if s.startswith("-") or s.startswith("+"):
        if has_parens or negative:
            return None
        if s.startswith("-"):
            negative = True
        s = s[1:].strip()

    clean_num: Optional[str] = None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind(".") and _EU_GROUPED_NUM_RE.match(s):
            clean_num = s.replace(".", "").replace(" ", "").replace("\u00a0", "").replace("'", "").replace(",", ".")
        elif s.rfind(".") > s.rfind(",") and _SPACE_US_GROUPED_NUM_RE.match(s):
            clean_num = s.replace(",", "").replace(" ", "").replace("\u00a0", "").replace("'", "")
    elif "," in s:
        if re.match(r"^\d{1,3}(?:,\d{3})+$", s):
            clean_num = s.replace(",", "")
        elif _EU_GROUPED_NUM_RE.match(s) and re.search(r",\d{1,2}$", s):
            clean_num = s.replace(".", "").replace(" ", "").replace("\u00a0", "").replace("'", "").replace(",", ".")
    elif _SPACE_US_GROUPED_NUM_RE.match(s):
        clean_num = s.replace(",", "").replace(" ", "").replace("\u00a0", "").replace("'", "")
    elif _EU_GROUPED_NUM_RE.match(s):
        clean_num = s.replace(".", "").replace(" ", "").replace("\u00a0", "").replace("'", "")

    if not clean_num:
        return None
    try:
        cents = int((Decimal(clean_num) * Decimal("100")).to_integral_exact())
    except (InvalidOperation, ValueError):
        return None
    return -cents if negative and cents != 0 else cents


def parse_canonical_phone(value: str) -> Optional[str]:
    """Normalize US 10-digit and +1 / international E.164 phone numbers, rejecting 9/11-digit typos."""
    s = _norm_dashes_ws(value)
    if not s or not _PHONE_ALLOWED_RE.match(s):
        return None
    if "+" in s[1:]:
        return None

    digits = re.sub(r"\D", "", s)
    if s.startswith("+"):
        if digits.startswith("1"):
            return f"+{digits}" if len(digits) == 11 else None
        return f"+{digits}" if 8 <= len(digits) <= 15 else None

    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        # Accept leading country code 1 only when formatted as a leading country code or valid NANP area code
        if re.match(r"^1(?:[-.\s]+|\s*\()", s) or (s.isdigit() and digits[1] in "23456789"):
            return f"+{digits}"
    return None


def match_masked_id(written: str, observed: str, *, min_mask_suffix: int = 4) -> bool:
    """Match masked identifier (e.g. XXX-XX-6789 or ***6789) against written source identifier."""
    if min_mask_suffix < 1:
        return False
    w_clean = re.sub(r"[\s\-]", "", _norm_dashes_ws(written)).upper()
    o_clean = re.sub(r"[\s\-]", "", _norm_dashes_ws(observed))
    if len(w_clean) < min_mask_suffix or not w_clean.isalnum() or not o_clean:
        return False

    if o_clean.isalnum() and o_clean.upper() == w_clean:
        return True

    m = _SYMBOL_MASK_RE.match(o_clean) or _X_MASK_RE.match(o_clean)
    if not m:
        return False

    prefix = m.group("prefix").upper()
    suffix = m.group("suffix").upper()
    if len(suffix) < min_mask_suffix:
        return False
    if len(w_clean) <= len(prefix) + len(suffix):
        return False
    return w_clean.startswith(prefix) and w_clean.endswith(suffix)


def extract_code_prefix_token(value: str) -> Optional[str]:
    """Extract canonical clinical/accounting code prefix while preserving modifiers and subcodes."""
    s = _norm_dashes_ws(value)
    if not s:
        return None
    s = re.sub(r"(?<=[A-Za-z0-9])\s*\.\s*(?=[A-Za-z0-9])", ".", s)

    # Strip trailing human-readable description after ' - ', ' | ', ': ', or ' ('
    delim_match = re.split(r"\s+[-|]\s+|\s*:\s+|\s+\(", s, maxsplit=1)
    if len(delim_match) == 2:
        lhs, rhs = delim_match[0].strip(), delim_match[1].strip()
        if re.search(r"[A-Za-z]{2,}", rhs):
            s = lhs

    m_spaced_id = _SPACED_PREFIX_CODE_RE.match(s)
    if m_spaced_id:
        s = f"{m_spaced_id.group(1)}-{m_spaced_id.group(2)}"
    else:
        m_prose = _CODE_PLUS_PROSE_RE.match(s)
        if m_prose:
            s = m_prose.group(1)

    token = s.upper()
    return token if _CODE_TOKEN_RE.match(token) else None


def canonical_equal(
    written: str,
    observed: str,
    field_type: FieldType,
    *,
    min_mask_suffix: int = 4,
) -> bool:
    """Deterministically compare written source value against post-commit observed read-back."""
    if field_type not in VALID_FIELD_TYPES:
        raise ValueError(f"Unsupported field_type: {field_type!r}")
    if written is None or observed is None:
        return False

    if field_type == "exact":
        return bool(written.strip()) and written == observed

    if field_type == "casefold_ws":
        nw = normalize_casefold_ws(written)
        no = normalize_casefold_ws(observed)
        return bool(nw) and nw == no

    if field_type == "date":
        dw = parse_canonical_date(written)
        do = parse_canonical_date(observed)
        return dw is not None and dw == do

    if field_type == "currency":
        cw = parse_canonical_currency_cents(written)
        co = parse_canonical_currency_cents(observed)
        return cw is not None and cw == co

    if field_type == "phone":
        pw = parse_canonical_phone(written)
        po = parse_canonical_phone(observed)
        return pw is not None and pw == po

    if field_type == "masked_id":
        return match_masked_id(written, observed, min_mask_suffix=min_mask_suffix)

    # field_type == "code_prefix"
    kw = extract_code_prefix_token(written)
    ko = extract_code_prefix_token(observed)
    return kw is not None and kw == ko


def canonical_var_name(name: str) -> str:
    clean = (name or "").strip()
    if not clean:
        return ""
    return clean if clean.startswith("$") else f"${clean}"


@dataclass(frozen=True)
class DossierVariableSchema:
    var_name: str
    field_type: FieldType
    role_tokens: FrozenSet[str]
    synonyms: FrozenSet[str] = field(default_factory=frozenset)

    def __init__(
        self,
        var_name: str,
        field_type: FieldType,
        role_tokens: Iterable[str],
        synonyms: Iterable[str] = (),
    ) -> None:
        if field_type not in VALID_FIELD_TYPES:
            raise ValueError(f"Unsupported field_type {field_type!r} for {var_name!r}")
        norm_tokens: set[str] = set()
        for tok in role_tokens:
            norm_tokens.update(tokenize_role_label(str(tok)))
        norm_syns: set[str] = set()
        for syn in synonyms:
            norm_syns.update(tokenize_role_label(str(syn)))
        object.__setattr__(self, "var_name", canonical_var_name(var_name))
        object.__setattr__(self, "field_type", field_type)
        object.__setattr__(self, "role_tokens", frozenset(norm_tokens))
        object.__setattr__(self, "synonyms", frozenset(norm_syns))


@dataclass(frozen=True)
class SlotBindingVerdict:
    control_idx: int
    bound_var: str
    control_label: str
    status: SlotBindingStatus
    reason: str = ""
    candidate_vars: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SemanticSlotVerificationReport:
    ok: bool
    verdicts: Tuple[SlotBindingVerdict, ...]
    ambiguous_slots: Tuple[int, ...]


def _coerce_schemas(
    raw_schemas: Union[
        Sequence[DossierVariableSchema],
        Mapping[str, Union[DossierVariableSchema, Mapping[str, Any]]],
    ],
) -> Dict[str, DossierVariableSchema]:
    out: Dict[str, DossierVariableSchema] = {}
    if isinstance(raw_schemas, Mapping):
        for k, spec in raw_schemas.items():
            if isinstance(spec, DossierVariableSchema):
                vname = canonical_var_name(spec.var_name or k)
                out[vname] = DossierVariableSchema(
                    vname, spec.field_type, spec.role_tokens, spec.synonyms
                )
            else:
                vname = canonical_var_name(str(spec.get("var_name") or k))
                out[vname] = DossierVariableSchema(
                    vname,
                    spec["field_type"],
                    spec.get("role_tokens") or (),
                    spec.get("synonyms") or (),
                )
    else:
        for spec in raw_schemas:
            out[spec.var_name] = spec
    return out


def _extract_control_labels(
    controls: Union[
        Sequence[ControlSchema],
        Mapping[int, Union[str, ControlSchema]],
    ],
) -> Dict[int, str]:
    labels: Dict[int, str] = {}
    if isinstance(controls, Mapping):
        for idx, item in controls.items():
            cidx = int(idx)
            if isinstance(item, ControlSchema):
                labels[cidx] = " ".join(
                    p for p in (item.label, item.placeholder, item.name) if p
                ).strip()
            else:
                labels[cidx] = str(item)
    else:
        for pos, ctrl in enumerate(controls):
            cidx = ctrl.control_index if ctrl.control_index >= 0 else pos
            labels[cidx] = " ".join(
                p for p in (ctrl.label, ctrl.placeholder, ctrl.name) if p
            ).strip()
    return labels


def verify_semantic_slot_bindings(
    slot_bindings: Mapping[int, str],
    variable_schemas: Union[
        Sequence[DossierVariableSchema],
        Mapping[str, Union[DossierVariableSchema, Mapping[str, Any]]],
    ],
    controls: Union[
        Sequence[ControlSchema],
        Mapping[int, Union[str, ControlSchema]],
    ],
    *,
    explicitly_disambiguated_slots: AbstractSet[int] = frozenset(),
) -> SemanticSlotVerificationReport:
    """Detect same-type variable ambiguity and role-token conflicts before commit."""
    schemas = _coerce_schemas(variable_schemas)
    labels_by_idx = _extract_control_labels(controls)

    by_type: Dict[FieldType, list[DossierVariableSchema]] = {}
    for sch in schemas.values():
        by_type.setdefault(sch.field_type, []).append(sch)

    # Precompute discriminating role tokens per variable within each multi-var field_type cohort
    discriminating_by_var: Dict[str, FrozenSet[str]] = {}
    for ftype, cohort in by_type.items():
        if len(cohort) < 2:
            sch = cohort[0]
            discriminating_by_var[sch.var_name] = (
                (sch.role_tokens | sch.synonyms) - _GENERIC_TYPE_TOKENS
            )
            continue
        for sch in cohort:
            own = (sch.role_tokens | sch.synonyms) - _GENERIC_TYPE_TOKENS
            others: set[str] = set()
            for peer in cohort:
                if peer.var_name != sch.var_name:
                    others.update((peer.role_tokens | peer.synonyms) - _GENERIC_TYPE_TOKENS)
            discriminating_by_var[sch.var_name] = frozenset(own - others)

    # Count how many controls bind each canonical $var
    var_use_counts: Dict[str, int] = {}
    for raw_var in slot_bindings.values():
        cvar = canonical_var_name(raw_var)
        var_use_counts[cvar] = var_use_counts.get(cvar, 0) + 1

    verdicts: list[SlotBindingVerdict] = []
    ambiguous: list[int] = []

    for cidx in sorted(slot_bindings.keys()):
        raw_var = slot_bindings[cidx]
        cvar = canonical_var_name(raw_var)
        if cidx not in labels_by_idx:
            verdicts.append(
                SlotBindingVerdict(
                    control_idx=cidx,
                    bound_var=cvar,
                    control_label="",
                    status="AMBIGUOUS_SLOT",
                    reason=f"unknown_control_index:{cidx}",
                    candidate_vars=(cvar,) if cvar in schemas else tuple(sorted(schemas.keys())),
                )
            )
            ambiguous.append(cidx)
            continue

        label = labels_by_idx[cidx]
        label_tokens = tokenize_role_label(label)

        if cvar not in schemas:
            verdicts.append(
                SlotBindingVerdict(
                    control_idx=cidx,
                    bound_var=cvar,
                    control_label=label,
                    status="AMBIGUOUS_SLOT",
                    reason=f"unknown_variable:{cvar}",
                    candidate_vars=tuple(sorted(schemas.keys())),
                )
            )
            ambiguous.append(cidx)
            continue

        sch = schemas[cvar]
        cohort = sorted(by_type[sch.field_type], key=lambda s: s.var_name)
        candidate_vars = tuple(s.var_name for s in cohort)

        if len(cohort) < 2:
            verdicts.append(
                SlotBindingVerdict(
                    control_idx=cidx,
                    bound_var=cvar,
                    control_label=label,
                    status="OK",
                    candidate_vars=candidate_vars,
                )
            )
            continue

        if var_use_counts.get(cvar, 0) > 1:
            verdicts.append(
                SlotBindingVerdict(
                    control_idx=cidx,
                    bound_var=cvar,
                    control_label=label,
                    status="AMBIGUOUS_SLOT",
                    reason=f"duplicate_same_type_binding:{cvar}",
                    candidate_vars=candidate_vars,
                )
            )
            ambiguous.append(cidx)
            continue

        own_disc = discriminating_by_var[cvar]
        if not own_disc:
            if cidx not in explicitly_disambiguated_slots:
                verdicts.append(
                    SlotBindingVerdict(
                        control_idx=cidx,
                        bound_var=cvar,
                        control_label=label,
                        status="AMBIGUOUS_SLOT",
                        reason="indistinguishable_same_type_schema",
                        candidate_vars=candidate_vars,
                    )
                )
                ambiguous.append(cidx)
                continue

        conflicting_peers: list[str] = []
        for peer in cohort:
            if peer.var_name == cvar:
                continue
            peer_disc = discriminating_by_var[peer.var_name]
            hit = sorted(label_tokens & peer_disc)
            if hit:
                conflicting_peers.extend(hit)

        if conflicting_peers:
            verdicts.append(
                SlotBindingVerdict(
                    control_idx=cidx,
                    bound_var=cvar,
                    control_label=label,
                    status="AMBIGUOUS_SLOT",
                    reason=f"conflicting_role_tokens:{','.join(sorted(set(conflicting_peers)))}",
                    candidate_vars=candidate_vars,
                )
            )
            ambiguous.append(cidx)
            continue

        has_own_disc = bool(label_tokens & own_disc)
        if not has_own_disc and cidx not in explicitly_disambiguated_slots:
            verdicts.append(
                SlotBindingVerdict(
                    control_idx=cidx,
                    bound_var=cvar,
                    control_label=label,
                    status="AMBIGUOUS_SLOT",
                    reason="missing_disambiguating_role_token",
                    candidate_vars=candidate_vars,
                )
            )
            ambiguous.append(cidx)
            continue

        verdicts.append(
            SlotBindingVerdict(
                control_idx=cidx,
                bound_var=cvar,
                control_label=label,
                status="OK",
                candidate_vars=candidate_vars,
            )
        )

    return SemanticSlotVerificationReport(
        ok=(len(ambiguous) == 0),
        verdicts=tuple(verdicts),
        ambiguous_slots=tuple(ambiguous),
    )
