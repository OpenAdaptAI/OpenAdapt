"""The ISO dates a row in a listed table could be showing."""

from __future__ import annotations

import re

#: Date-looking text in a listed row: ISO or Y/M/D, numeric with / . or -,
#: and a spelled-out month either side of the day.
_ROW_DATE_PATTERNS = (
    r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b",
    r"\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b",
    r"\b\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4}\b",
    r"\b[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}\b",
)


def _row_dates(row: str) -> set[str]:
    """Every ISO date a listed row could be showing.

    Each date-looking substring is read the way intake reads a date
    (``intake.extract.normalize_date``). One that reads either way (03/04/1970)
    contributes both readings; one that isn't a date contributes nothing.
    """

    from datetime import date as _date

    from openadapt_flow.intake.extract import normalize_date

    found: set[str] = set()
    for pattern in _ROW_DATE_PATTERNS:
        for match in re.finditer(pattern, row):
            text = match.group(0)
            ymd = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
            if ymd:
                try:
                    found.add(
                        _date(
                            int(ymd.group(1)), int(ymd.group(2)), int(ymd.group(3))
                        ).isoformat()
                    )
                except ValueError:
                    pass
                continue
            text = re.sub(r"(\d)\.(?=\d)", r"\1/", text).replace(".", "")
            iso, error, _ = normalize_date(text)
            if iso is not None:
                found.add(iso)
            elif error and "ambiguous" in error:
                for order in ("MM/DD/YYYY", "DD/MM/YYYY"):
                    reading = normalize_date(text, order)[0]
                    if reading is not None:
                        found.add(reading)
    return found
