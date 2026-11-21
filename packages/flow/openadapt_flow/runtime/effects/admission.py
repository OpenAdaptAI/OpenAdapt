"""The safety bar: a relaxation is admitted only if planted faults still fail.

A learned mask (a ``frame.learned`` excuse) or a tier relaxation (a
``frame.database.entity_tables`` scope, under which rows outside the entity
tables are logged instead of stopping a write) makes the verifier accept
more. Each is admitted only with evidence that the planted faults are still
caught under it: the same fault cases, judged by the relaxed contracts.

The bar (``SAFETY_BAR``): a wrong row, a wrong value, a duplicate, an edit
to a sibling business record, a late revert of the target and a lost update.
A relaxation of the acting user's own table must also still stop another
user's row and another column of the actor's row (``ACTOR_ATTACKS``).

Admitted means: every required class has at least one case, every case is
caught, and every case caught before the relaxation is caught after it (a
case that flips to passing is named). Anything else is refused, and the
refusal says which class or case. The results are the caller's, from
``openadapt-flow qualify`` or a fault replay; this module only decides.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = "openadapt.relaxation-faults/v1"

SAFETY_BAR: tuple[str, ...] = (
    "wrong_row",
    "wrong_value",
    "duplicate",
    "sibling_edit",
    "late_revert",
    "lost_update",
)
#: Faults aimed at the acting user's table: another user's row moved, and a
#: column of the actor's own row that no excuse names.
ACTOR_ATTACKS: tuple[str, ...] = ("other_user_row", "actor_other_column")

#: ``fault_gate`` class names that are the same fault as a bar class.
ALIASES: dict[str, str] = {
    "wrong_record": "wrong_row",
    "unrelated_edit": "sibling_edit",
    "other_record": "sibling_edit",
    "revert": "late_revert",
    "late_write": "late_revert",
    "concurrent_save": "lost_update",
}


class AdmissionRefused(ValueError):
    pass


@dataclass(frozen=True)
class FaultResult:
    case: str
    fault_class: str
    caught: bool

    @property
    def bar_class(self) -> str:
        return ALIASES.get(self.fault_class, self.fault_class)


@dataclass
class Admission:
    admitted: bool
    relaxation: str
    reasons: list[str] = field(default_factory=list)
    per_class: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "relaxation": self.relaxation,
            "reasons": list(self.reasons),
            "per_class": self.per_class,
        }


def admit(
    relaxation: str,
    after: Sequence[FaultResult],
    before: Sequence[FaultResult] = (),
    *,
    required: Sequence[str] = SAFETY_BAR,
) -> Admission:
    """Decide one relaxation from its fault results (see the module doc)."""

    reasons: list[str] = []
    per: dict[str, dict[str, int]] = {}
    for r in after:
        c = per.setdefault(r.bar_class, {"cases": 0, "caught": 0})
        c["cases"] += 1
        c["caught"] += int(bool(r.caught))
    for cls in required:
        if per.get(cls, {}).get("cases", 0) == 0:
            reasons.append(f"no planted {cls} case was run under the relaxation")
    for r in after:
        if not r.caught:
            reasons.append(
                f"planted {r.bar_class} case {r.case} passed under the relaxation"
            )
    now = {r.case: r.caught for r in after}
    for r in before:
        # A case caught before that passes now is named above.
        if r.caught and r.case not in now:
            reasons.append(
                f"case {r.case}, caught before, was not run under the relaxation"
            )
    return Admission(not reasons, relaxation, reasons, per)


def load_results(path: Path) -> tuple[str, list[FaultResult]]:
    """(proposal digest the faults ran under, results) from a results file:
    ``{"schema": SCHEMA, "proposal_sha256": ..., "cases": [{case, class,
    caught}]}``."""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, Mapping) or data.get("schema") != SCHEMA:
        raise AdmissionRefused(f"{path} is not a {SCHEMA} results file")
    out = []
    for item in data.get("cases") or []:
        if not isinstance(item, Mapping):
            continue
        out.append(
            FaultResult(str(item["case"]), str(item["class"]), bool(item["caught"]))
        )
    return str(data.get("proposal_sha256") or ""), out


def digest(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
