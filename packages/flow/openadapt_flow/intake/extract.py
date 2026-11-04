"""Propose, validate, and confirm workflow parameters from an intake document."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping, Optional

from openadapt_flow.intake.document import IntakeDocument
from openadapt_flow.ir import ParamKind, ParamSpec

#: Fields whose confidence is below this need a person to confirm them.
DEFAULT_CONFIRM_THRESHOLD = 0.9

#: Dates whose month is spelled out: never ambiguous.
_NAMED_MONTH_FORMATS = (
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%B %d %Y",
)
#: A source's declared order for numeric dates such as 03/04/1970.
DATE_ORDERS = ("MM/DD/YYYY", "DD/MM/YYYY")
_TRUE = {"yes", "y", "true", "1", "x", "checked"}
_FALSE = {"no", "n", "false", "0", "unchecked"}
_SEP = r"\s*(?:[:#=]|-\s)\s*"


class IntakeNotConfirmed(RuntimeError):
    """Raised when parameters are requested before every field is resolved."""


@dataclass
class ExtractedField:
    name: str
    value: Optional[str]
    confidence: float
    source_line: Optional[int] = None
    label: Optional[str] = None
    error: Optional[str] = None
    confirmed: bool = False
    #: The text as the document wrote it, when it differs from ``value``.
    raw: Optional[str] = None
    #: How ``raw`` became ``value`` (for example a date's conversion to ISO).
    conversion: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "confidence": round(self.confidence, 3),
            "source_line": self.source_line,
            "label": self.label,
            "error": self.error,
            "confirmed": self.confirmed,
            "raw": self.raw,
            "conversion": self.conversion,
        }


@dataclass
class IntakeResult:
    """Proposed values for every parameter, plus what still needs a person."""

    fields: dict[str, ExtractedField]
    specs: dict[str, ParamSpec]
    threshold: float = DEFAULT_CONFIRM_THRESHOLD
    mode: str = "local"
    notes: list[str] = field(default_factory=list)
    #: Declared order of numeric dates, per parameter (``DATE_ORDERS``).
    date_formats: dict[str, str] = field(default_factory=dict)

    def needs_confirmation(self) -> list[str]:
        """Names of fields a person must confirm or correct before running."""
        out = []
        for name, f in self.fields.items():
            spec = self.specs[name]
            if f.confirmed:
                continue
            if f.value is None and not spec.required:
                continue
            if f.error or f.value is None or f.confidence < self.threshold:
                out.append(name)
        return out

    def confirm(self, name: str, value: Optional[str] = None) -> ExtractedField:
        """Accept the proposed value, or replace it with ``value``.

        The value is validated against the spec; an invalid value raises
        ``ValueError`` and the field stays unconfirmed.
        """
        f = self.fields[name]
        candidate = f.value if value is None else value
        if candidate is None:
            raise ValueError(f"{name}: no value to confirm")
        normalized, error, conversion = convert_value(
            self.specs[name], candidate, date_format=self.date_formats.get(name)
        )
        if error:
            raise ValueError(f"{name}: {error}")
        f.value, f.error, f.confirmed, f.confidence = normalized, None, True, 1.0
        f.raw = candidate.strip() if conversion else None
        f.conversion = conversion
        return f

    def to_params(self) -> dict[str, str]:
        """Return a ``--params-file`` object; refuses while fields are pending."""
        pending = self.needs_confirmation()
        if pending:
            raise IntakeNotConfirmed("confirm before running: " + ", ".join(pending))
        return {n: f.value for n, f in self.fields.items() if f.value is not None}

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "threshold": self.threshold,
            "needs_confirmation": self.needs_confirmation(),
            "fields": [f.to_dict() for f in self.fields.values()],
            "notes": list(self.notes),
        }


def normalize_date(
    raw: str, date_format: Optional[str] = None
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """A date as a canonical ISO date: ``(iso, error, conversion)``.

    - An ISO date is checked and kept (no conversion).
    - A date with the month spelled out converts.
    - A numeric date (``03/04/1970``, ``03-04-1970``) converts when its order
      is declared (``date_format``, one of ``DATE_ORDERS``), or can be told
      from the date itself: one part over 12 is the day, and equal parts read
      the same either way. Otherwise it is ambiguous and refused.
    - ``YYYY/MM/DD`` converts.
    - Anything else, or a date that doesn't exist (1970-02-30), is refused.
    """

    value = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value).isoformat(), None, None
        except ValueError:
            return None, f"not a valid date: {value!r}", None
    numeric = re.fullmatch(r"(\d{1,2})([/-])(\d{1,2})\2(\d{4})", value)
    if numeric:
        a, b, year = int(numeric.group(1)), int(numeric.group(3)), int(numeric.group(4))
        if date_format is not None:
            if date_format not in DATE_ORDERS:
                return None, f"unknown declared date format {date_format!r}", None
            month_first, why = date_format == "MM/DD/YYYY", "declared"
        elif a > 12 >= b:
            month_first, why = False, f"{a} can only be the day"
        elif b > 12 >= a:
            month_first, why = True, f"{b} can only be the day"
        elif a == b:
            month_first, why = True, "day and month are equal"
        else:
            return (
                None,
                f"ambiguous date {value!r}: day and month can't be told apart; "
                "declare the source's format (MM/DD/YYYY or DD/MM/YYYY)",
                None,
            )
        month, day = (a, b) if month_first else (b, a)
        try:
            parsed = date(year, month, day)
        except ValueError:
            return None, f"not a valid date: {value!r}", None
        order = "MM/DD/YYYY" if month_first else "DD/MM/YYYY"
        return parsed.isoformat(), None, f"{order} to ISO ({why})"
    ymd = re.fullmatch(r"(\d{4})/(\d{1,2})/(\d{1,2})", value)
    if ymd:
        try:
            parsed = date(int(ymd.group(1)), int(ymd.group(2)), int(ymd.group(3)))
        except ValueError:
            return None, f"not a valid date: {value!r}", None
        return parsed.isoformat(), None, "YYYY/MM/DD to ISO"
    for fmt in _NAMED_MONTH_FORMATS:
        try:
            named: date = datetime.strptime(value, fmt).date()
        except ValueError:
            continue
        return named.isoformat(), None, "month name to ISO"
    return None, f"not a recognized date: {value!r}", None


def convert_value(
    spec: ParamSpec, raw: str, *, date_format: Optional[str] = None
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Normalize ``raw`` for ``spec``: ``(value, error, conversion)``."""

    if spec.type == ParamKind.DATE and raw.strip():
        iso, error, conversion = normalize_date(raw, date_format)
        if error:
            return raw.strip(), error, None
        return iso, None, conversion
    value, error = validate_value(spec, raw)
    return value, error, None


def validate_value(
    spec: ParamSpec, raw: str, *, date_format: Optional[str] = None
) -> tuple[Optional[str], Optional[str]]:
    """Normalize ``raw`` for ``spec``. Returns ``(value, error)``."""
    value = raw.strip()
    if not value:
        return None, "empty value"
    kind = spec.type
    if kind == ParamKind.DATE:
        iso, error, _conversion = normalize_date(value, date_format)
        return (value, error) if error else (iso, None)
    if kind == ParamKind.NUMBER:
        cleaned = value.replace(",", "")
        try:
            float(cleaned)
        except ValueError:
            return value, f"not a number: {value!r}"
        return cleaned, None
    if kind == ParamKind.BOOLEAN:
        low = value.lower()
        if low in _TRUE:
            return "true", None
        if low in _FALSE:
            return "false", None
        return value, f"not a yes/no value: {value!r}"
    if kind == ParamKind.ENUM and spec.choices:
        for choice in spec.choices:
            if choice.lower() == value.lower():
                return choice, None
        return value, f"{value!r} is not one of {spec.choices}"
    return value, None


def _labels_for(name: str, aliases: Mapping[str, list[str]]) -> list[tuple[str, float]]:
    """Candidate labels for a param, each with the confidence a match earns."""
    words = name.replace("-", "_").split("_")
    base = " ".join(words)
    out = [(base, 0.95)]
    expansions = {
        "no": "number",
        "num": "number",
        "dob": "date of birth",
        "pt": "patient",
    }
    expanded = " ".join(expansions.get(w, w) for w in words)
    if expanded != base:
        out.append((expanded, 0.95))
    out += [(a.lower(), 0.95) for a in aliases.get(name, [])]
    # A label that only contains the param's last word ("Name" for patient_name)
    # is a weaker signal.
    if len(words) > 1 and words[-1] not in expansions:
        out.append((words[-1], 0.6))
    return out


def _match_label(line: str, label: str) -> Optional[str]:
    pattern = (
        r"(?:^|\b)"
        + r"[\s_.]*".join(map(re.escape, label.split()))
        + _SEP
        + r"(.+?)\s*$"
    )
    m = re.search(pattern, line, re.I)
    return m.group(1) if m else None


def extract_fields(
    doc: IntakeDocument,
    specs: Mapping[str, ParamSpec],
    *,
    aliases: Optional[Mapping[str, list[str]]] = None,
    threshold: float = DEFAULT_CONFIRM_THRESHOLD,
    date_formats: Optional[Mapping[str, str]] = None,
) -> IntakeResult:
    """Local, deterministic extraction by label matching. No model call.

    ``date_formats`` declares the order of numeric dates per parameter
    (``DATE_ORDERS``); without it an ambiguous date needs a person.
    """
    aliases = aliases or {}
    formats = dict(date_formats or {})
    fields: dict[str, ExtractedField] = {}
    for name, spec in specs.items():
        hits: list[tuple[float, int, str, str]] = []
        for label, conf in _labels_for(name, aliases):
            for i, line in enumerate(doc.lines):
                raw = _match_label(line, label)
                if raw:
                    hits.append((conf, i, label, raw))
        if not hits:
            fields[name] = ExtractedField(
                name, None, 0.0, error="no matching label found"
            )
            continue
        best = max(h[0] for h in hits)
        top = [h for h in hits if h[0] == best]
        conf, line_no, label, raw = top[0]
        fmt = formats.get(name)
        value, error, conversion = convert_value(spec, raw, date_format=fmt)
        distinct = {convert_value(spec, h[3], date_format=fmt)[0] for h in top}
        if len(distinct) > 1:
            conf = min(conf, 0.5)
            error = error or f"{len(distinct)} different values found"
        if error:
            conf = 0.0
        fields[name] = ExtractedField(
            name,
            value,
            conf,
            line_no + 1,
            label,
            error,
            raw=raw.strip() if conversion else None,
            conversion=conversion,
        )
    return IntakeResult(fields, dict(specs), threshold, "local", date_formats=formats)


def build_agent_request(
    doc: IntakeDocument, specs: Mapping[str, ParamSpec]
) -> dict[str, Any]:
    """A request the calling agent can answer, with identifiers scrubbed.

    Each line is scrubbed with openadapt-privacy. The agent sees
    placeholders, not identifiers, and answers with the line number that
    carries each field. Without openadapt-privacy, or when the scrubber fails
    on a line, this raises and nothing is printed; it never falls back to
    plaintext, whatever ``OPENADAPT_FLOW_SCRUB`` says.
    """
    from openadapt_flow import privacy

    if privacy.scrub_mode() == "off":
        raise RuntimeError(
            "agent intake requires scrubbing; OPENADAPT_FLOW_SCRUB is off"
        )
    try:
        scrubber = privacy.require_outbound_scrubber("the intake agent request")
    except privacy.PrivacyNotAvailable as exc:
        raise RuntimeError(
            "agent intake requires openadapt-privacy: pip install 'openadapt-flow[privacy]'"
        ) from exc
    lines = [
        f"{i + 1}: {privacy.scrub_outbound_text(ln, scrubber)}"
        for i, ln in enumerate(doc.lines)
    ]
    return {
        "instructions": (
            "For each field, return the 1-based line number whose text carries "
            "the value, and the label text that precedes the value on that line "
            "(empty if none). Identifiers are replaced by placeholders; do not "
            "guess them. Use null when no line carries the field. Answer as JSON: "
            '{"fields": {"<name>": {"line": <int|null>, "label": "<str>", '
            '"confidence": <0..1>}}}'
        ),
        "fields": [
            {
                "name": s.name or n,
                "type": s.type.value,
                "required": s.required,
                "choices": s.choices,
            }
            for n, s in specs.items()
        ],
        "document": lines,
    }


def apply_agent_response(
    doc: IntakeDocument,
    specs: Mapping[str, ParamSpec],
    response: Mapping[str, Any],
    *,
    threshold: float = DEFAULT_CONFIRM_THRESHOLD,
) -> IntakeResult:
    """Read values locally from the lines the agent pointed at."""
    answers = response.get("fields") or {}
    fields: dict[str, ExtractedField] = {}
    for name, spec in specs.items():
        ans = answers.get(name) or {}
        line = ans.get("line")
        if not isinstance(line, int) or not 1 <= line <= len(doc.lines):
            fields[name] = ExtractedField(
                name, None, 0.0, error="agent gave no valid line"
            )
            continue
        text = doc.lines[line - 1]
        label = str(ans.get("label") or "").strip()
        raw = _match_label(text, label) if label else None
        if raw is None:
            raw = re.split(_SEP, text, maxsplit=1)[-1] if label else text
        try:
            conf = float(ans.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        # The agent never saw the value, so its confidence is capped below the
        # default threshold unless the label matched exactly on that line.
        if not label or _match_label(text, label) is None:
            conf = min(conf, 0.7)
        value, error, conversion = convert_value(spec, raw)
        fields[name] = ExtractedField(
            name,
            value,
            0.0 if error else max(0.0, min(conf, 1.0)),
            line,
            label or None,
            error,
            raw=raw.strip() if conversion else None,
            conversion=conversion,
        )
    return IntakeResult(fields, dict(specs), threshold, "agent")
