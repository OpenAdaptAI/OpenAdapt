"""Plain results derived from Flow's transaction outcome.

Every operator-facing result, halt label, and next step in Desktop comes from
:func:`plain_result`. The rule is the business-layer safety rule: a label may
say "nothing was written" or "safe to try again" only when the engine's
``transaction_outcome`` proves that no business effect happened. The coarse
``execution_outcome`` alone never proves that, so a coarse ``HALTED`` without a
transaction outcome becomes "Check the record", never "Stopped before saving".

The frontend mirrors this table in ``src/lib/outcomes.ts``. Both read the same
cases from ``src/lib/outcome-cases.json`` in their tests, so the two copies
cannot drift apart silently.
"""

from __future__ import annotations

import ast
import re
from typing import Any

#: Transaction outcomes Flow may stamp on a report (openadapt_flow.transaction).
TRANSACTION_OUTCOMES = frozenset(
    {
        "VERIFIED",
        "HALTED_BEFORE_EFFECT",
        "RECONCILIATION_REQUIRED",
        "FAILED_PLATFORM",
        "CANCELED",
        "REJECTED_POLICY",
        "COMPLETED_UNVERIFIED",
        "ROLLED_BACK",
    }
)

#: Transaction outcomes that prove no business effect happened.
NO_EFFECT_TRANSACTIONS = frozenset(
    {"HALTED_BEFORE_EFFECT", "REJECTED_POLICY", "CANCELED", "FAILED_PLATFORM"}
)

# Which precise execution outcome each transaction outcome may accompany.
# Flow derives the transaction outcome from the execution outcome
# (openadapt_flow.transaction.classify_transaction_outcome); any other pairing
# means the report is not internally consistent and proves nothing.
_CONSISTENT_EXECUTION = {
    "VERIFIED": {"VERIFIED"},
    "HALTED_BEFORE_EFFECT": {"HALTED"},
    "REJECTED_POLICY": {"HALTED", "FAILED"},
    "CANCELED": {"HALTED", "FAILED"},
    "FAILED_PLATFORM": {"FAILED"},
    "COMPLETED_UNVERIFIED": {"COMPLETED_UNVERIFIED"},
    "ROLLED_BACK": {"ROLLED_BACK"},
    "RECONCILIATION_REQUIRED": {
        "HALTED",
        "FAILED",
        "COMPLETED_UNVERIFIED",
        "ROLLED_BACK",
    },
}

#: Plain result keys. The frontend renders the words.
DONE_CHECKED = "done_checked"
STOPPED_BEFORE_SAVING = "stopped_before_saving"
CHECK_RECORD = "check_record"
FINISHED_UNCHECKED = "finished_unchecked"
DID_NOT_FINISH = "did_not_finish"
DID_NOT_START = "did_not_start"

PLAIN_RESULT_KEYS = frozenset(
    {
        DONE_CHECKED,
        STOPPED_BEFORE_SAVING,
        CHECK_RECORD,
        FINISHED_UNCHECKED,
        DID_NOT_FINISH,
        DID_NOT_START,
    }
)


def consistent_transaction_outcome(
    execution_outcome: Any,
    transaction_outcome: Any,
) -> str | None:
    """Return the transaction outcome only when it matches the execution outcome."""

    if not isinstance(transaction_outcome, str):
        return None
    if transaction_outcome not in TRANSACTION_OUTCOMES:
        return None
    allowed = _CONSISTENT_EXECUTION[transaction_outcome]
    if execution_outcome not in allowed:
        return None
    return transaction_outcome


def plain_result(
    execution_outcome: Any,
    transaction_outcome: Any = None,
    *,
    pre_action_refusal: bool = False,
) -> dict[str, Any]:
    """Map one run's outcome to a plain result key and its safety flags.

    ``nothing_written`` and ``safe_to_retry`` are True only when the evidence
    proves that no business effect happened.
    """

    if pre_action_refusal:
        # The dispatcher returns a pre-action refusal only when Flow was never
        # invoked, so nothing reached the application.
        return _result(DID_NOT_START, nothing_written=True, exact="refused before Flow")

    tx = consistent_transaction_outcome(execution_outcome, transaction_outcome)
    if transaction_outcome is not None and tx is None:
        # A transaction outcome that disagrees with the execution outcome is
        # not evidence of anything.
        return _result(
            CHECK_RECORD,
            may_have_written=True,
            exact=f"inconsistent {execution_outcome}/{transaction_outcome}",
        )
    if tx == "VERIFIED":
        return _result(DONE_CHECKED, may_have_written=True, exact=tx)
    if tx in {"HALTED_BEFORE_EFFECT", "REJECTED_POLICY", "CANCELED"}:
        return _result(STOPPED_BEFORE_SAVING, nothing_written=True, exact=tx)
    if tx == "FAILED_PLATFORM":
        return _result(DID_NOT_FINISH, nothing_written=True, exact=tx)
    if tx == "COMPLETED_UNVERIFIED":
        return _result(FINISHED_UNCHECKED, may_have_written=True, exact=tx)
    if tx in {"RECONCILIATION_REQUIRED", "ROLLED_BACK"}:
        return _result(CHECK_RECORD, may_have_written=True, exact=tx)

    # No transaction outcome: only the coarse execution outcome is known.
    if execution_outcome == "VERIFIED":
        # Desktop accepts VERIFIED only after FlowBridge validated the exact
        # outcome envelope, and Flow's only production success is VERIFIED.
        return _result(DONE_CHECKED, may_have_written=True, exact="VERIFIED")
    if execution_outcome in {"COMPLETED_UNVERIFIED", "success"}:
        return _result(
            FINISHED_UNCHECKED, may_have_written=True, exact=str(execution_outcome)
        )
    if execution_outcome == "FAILED":
        return _result(DID_NOT_FINISH, may_have_written=True, exact="FAILED")
    return _result(
        CHECK_RECORD,
        may_have_written=True,
        exact=str(execution_outcome or "unknown"),
    )


def _result(
    key: str,
    *,
    nothing_written: bool = False,
    may_have_written: bool = False,
    exact: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "nothing_written": nothing_written,
        "may_have_written": may_have_written and not nothing_written,
        "safe_to_retry": nothing_written,
        "needs_person": key != DONE_CHECKED,
        "exact": exact,
    }


def plain_result_for_transaction(transaction_outcome: Any) -> dict[str, Any]:
    """Plain result for a record that carries only Flow's transaction outcome.

    The hosted runner mirrors Flow's closed terminal transaction outcome into
    local history without the execution outcome. An unknown value is never
    evidence of anything.
    """

    execution = {
        "VERIFIED": "VERIFIED",
        "HALTED_BEFORE_EFFECT": "HALTED",
        "REJECTED_POLICY": "HALTED",
        "CANCELED": "FAILED",
        "FAILED_PLATFORM": "FAILED",
        "COMPLETED_UNVERIFIED": "COMPLETED_UNVERIFIED",
        "ROLLED_BACK": "ROLLED_BACK",
        "RECONCILIATION_REQUIRED": "HALTED",
    }.get(str(transaction_outcome))
    if execution is None:
        return plain_result("unknown")
    return plain_result(execution, transaction_outcome)


#: At most this many fields, each value cut to this many characters.
MAX_FIELD_DIFFS = 8
MAX_FIELD_VALUE = 120

# Flow's record check writes a refuted field as one verdict line:
#   field 'dob' is '1960-01-01', expected '1949-12-11' (...)
# Values are Python reprs, so either quote style can appear.
_REPR = r"(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"
_FIELD_LINE = re.compile(
    rf"field '(?P<field>[^']{{1,128}})' is (?P<observed>{_REPR}), "
    rf"expected (?P<expected>{_REPR})"
)


def _unrepr(text: str) -> str | None:
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None
    return value if isinstance(value, str) else None


def field_diffs(report: dict) -> list[dict[str, str]]:
    """The fields a record check found different from what was typed.

    Each entry is ``{"field", "typed", "on_file"}``. Read from Flow's
    structured ``effect_field_checks`` when a result carries them, otherwise
    from the record check's verdict lines in ``effect_results``. These values
    can identify a patient: they are for the local screen only.
    """

    results = report.get("results")
    if not isinstance(results, list):
        return []
    diffs: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(field: object, typed: object, on_file: object) -> None:
        if not isinstance(field, str) or not field or field in seen:
            return
        if not isinstance(typed, str) or not isinstance(on_file, str):
            return
        if typed == on_file or len(diffs) >= MAX_FIELD_DIFFS:
            return
        seen.add(field)
        diffs.append(
            {
                "field": field[:MAX_FIELD_VALUE],
                "typed": typed[:MAX_FIELD_VALUE],
                "on_file": on_file[:MAX_FIELD_VALUE],
            }
        )

    for result in results:
        if not isinstance(result, dict):
            continue
        checks = result.get("effect_field_checks")
        if isinstance(checks, list) and checks:
            for check in checks:
                if isinstance(check, dict) and check.get("matched") is False:
                    add(check.get("field"), check.get("expected"), check.get("observed"))
            continue
        lines = result.get("effect_results")
        if not isinstance(lines, list):
            continue
        for line in lines:
            if not isinstance(line, str):
                continue
            for match in _FIELD_LINE.finditer(line):
                add(
                    match.group("field"),
                    _unrepr(match.group("expected")),
                    _unrepr(match.group("observed")),
                )
    return diffs


def halt_kind(report: dict) -> str | None:
    """Classify why a run stopped, for the plain question a person answers.

    ``wrong_record``: the right-record check saw a different record.
    ``screen_changed``: a step's target could not be found on screen.
    ``save_not_confirmed``: the record check refuted or could not confirm a save.
    ``other``: any other stop.
    Returns None when the run did not stop.
    """

    results = report.get("results")
    halt = report.get("halt")
    if not isinstance(results, list):
        results = []
    for result in results:
        if not isinstance(result, dict):
            continue
        identity = result.get("identity")
        if isinstance(identity, dict) and identity.get("status") == "mismatch":
            return "wrong_record"
    for result in results:
        if not isinstance(result, dict):
            continue
        if result.get("effect_verified") is False:
            return "save_not_confirmed"
    stopped = isinstance(halt, dict) and bool(halt)
    if not stopped and report.get("execution_outcome") not in {"HALTED", "FAILED"}:
        return None
    for result in results:
        if not isinstance(result, dict) or result.get("ok"):
            continue
        if result.get("resolution") is None and not result.get("skipped"):
            return "screen_changed"
    return "other"
