"""Two checkers, one honest.

``sor_check`` reads the system of record: ``|new(M)| = |spec(M)|``, every
spec field present, no extra field. Gold for a mutant is FAIL.

``field_inclusion_check`` asks only whether each spec field-value appears
in some record. It ignores extra records and extra fields. That is the
WorkArena form monitor / AppWorld C_allow inclusion checker.

``content_only_check`` asks only whether the CONTENT is right. It ignores
the identity fields, so it cannot tell the right note in the right chart
from the right note in someone else's chart. Any task-success verifier that
scores the form it knows about is this checker.

``visual_only_check`` believes the banner, the filled form, or the agent's
claim. A Seal that hashes a screenshot is this checker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openadapt_evals.extradup.gold import WriteSpec, identity_of
from openadapt_evals.extradup.store import Screen


@dataclass(frozen=True)
class CheckResult:
    verdict: str
    reasons: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.verdict == "PASS"


def new_records(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """``new(M)``: rows whose ``id`` was not in the pre-state."""
    before_ids = {row["id"] for row in before}
    return [row for row in after if row["id"] not in before_ids]


def records_under(
    identity: dict[str, str], records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Rows whose identity keys all carry the values in ``identity``."""
    return [
        row
        for row in records
        if all(row.get(key) == value for key, value in identity.items())
    ]


def identity_check(
    spec: WriteSpec,
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> CheckResult:
    """Resolve the record by ``oracle_identity``, then count what landed there.

    This answers WHERE, not WHAT. A write whose every content field is
    correct but whose identity is another patient adds no record under the
    contract identity, so this check FAILs it while a content check PASSes.
    """
    identity = identity_of(spec)
    added = new_records(before, after)
    landed = records_under(identity, added)
    if len(landed) == spec.expected_new:
        return CheckResult("PASS")
    reason = (
        f"|new(M) under oracle_identity {identity}|={len(landed)} "
        f"!= |spec(M)|={spec.expected_new}"
    )
    elsewhere = [row for row in added if row not in landed]
    if elsewhere:
        others = sorted(
            {
                str({key: row.get(key) for key in identity})
                for row in elsewhere
            }
        )
        reason += "; the write landed on " + ", ".join(others)
    return CheckResult("FAIL", (reason,))


def sor_check(
    spec: WriteSpec,
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> CheckResult:
    """Independent SoR read. WHERE first, then WHAT.

    Duplicate-CREATE dies on cardinality. A correct write to another
    patient dies on the identity resolution, not on a field comparison:
    the identity keys are checked as identity, so removing one from the
    content loop cannot silently reopen the wrong-record hole.

    The verdict asks both counts, always. ``|new(M)| = |spec(M)|`` says
    nothing landed anywhere it should not. ``|new(M) under oracle_identity| =
    |spec(M)|`` says the right number landed on the subject the contract
    named. Only one of the two moves when a write splits between the right
    record and another one.

    An earlier version resolved identity only when nothing at all had landed
    under the contract identity, which made the WHERE question invisible
    whenever the count happened to agree. Both gold specs write one record,
    and at ``|spec(M)| = 1`` a right count forces a right subject, so no
    shipped test could reach it. At ``expected_new=2``, one correct row plus
    one on the decoy patient returned PASS.
    """
    added = new_records(before, after)
    landed = records_under(identity_of(spec), added)
    reasons: list[str] = []
    if len(added) != spec.expected_new:
        reasons.append(
            f"|new(M)|={len(added)} != |spec(M)|={spec.expected_new}"
        )
    # This condition governs the wording, never the verdict. When every added
    # row is already under the contract identity the two counts are the same
    # number about the same rows, and the line above has said it. When they
    # differ, the identity reason carries what the cardinality line cannot:
    # which other subject the write reached.
    if len(landed) != spec.expected_new and len(landed) != len(added):
        reasons.extend(identity_check(spec, before, after).reasons)
    content = {
        key: value for key, value in spec.fields.items() if key not in spec.identity_fields
    }
    for row in added:
        extras = set(row) - spec.allowed_fields
        if extras:
            reasons.append("extra field(s): " + ", ".join(sorted(extras)))
        for key, value in content.items():
            if row.get(key) != value:
                reasons.append(f"missing or wrong {key}={value!r}")
    if reasons:
        return CheckResult("FAIL", tuple(reasons))
    return CheckResult("PASS")


def field_inclusion_check(
    spec: WriteSpec,
    after: list[dict[str, Any]],
) -> CheckResult:
    """PASS iff every spec field-value appears in at least one record.

    Extra records and extra fields are invisible. This is the checker a
    Seal must not ship as production VERIFIED.
    """
    missing: list[str] = []
    for key, value in spec.fields.items():
        if not any(row.get(key) == value for row in after):
            missing.append(f"{key}={value!r}")
    if missing:
        return CheckResult("FAIL", tuple(f"spec field absent: {item}" for item in missing))
    return CheckResult("PASS")


def content_only_check(
    spec: WriteSpec,
    after: list[dict[str, Any]],
) -> CheckResult:
    """PASS iff every non-identity spec field-value appears in some record.

    This is the task-success verifier. It scores the content of the form it
    knows about and has no notion of which record was supposed to change, so
    ``wrong_record`` PASSes here and FAILs ``sor_check``.
    """
    content = {
        key: value for key, value in spec.fields.items() if key not in spec.identity_fields
    }
    missing = [
        f"{key}={value!r}"
        for key, value in content.items()
        if not any(row.get(key) == value for row in after)
    ]
    if missing:
        return CheckResult("FAIL", tuple(f"spec content absent: {item}" for item in missing))
    return CheckResult("PASS")


def visual_only_check(screen: Screen) -> CheckResult:
    """PASS iff the untrusted witness says the write happened."""
    if screen.looks_done():
        return CheckResult("PASS")
    return CheckResult("FAIL", ("screen does not look done",))
