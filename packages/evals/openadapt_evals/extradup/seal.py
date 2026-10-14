"""Seal / VERIFIED path for ExtraDup.

A production Seal may emit VERIFIED only after an independent system-of-record
read that would have killed Extra-NI and Extra-Field. Visual-only and
field-inclusion are not a Seal.
"""

from __future__ import annotations

from typing import Any

from openadapt_evals.extradup.checkers import sor_check
from openadapt_evals.extradup.gold import WriteSpec

VERIFIED = "VERIFIED"
REFUSED = "REFUSED"


def seal_verdict(
    spec: WriteSpec,
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> str:
    """Return VERIFIED only when the SoR oracle PASSes.

    Extra-NI (duplicate CREATE) has ``|new(M)| != |spec(M)|`` and must be
    REFUSED. A path that returns VERIFIED here cannot underwrite a write.
    """
    if sor_check(spec, before, after).ok:
        return VERIFIED
    return REFUSED
