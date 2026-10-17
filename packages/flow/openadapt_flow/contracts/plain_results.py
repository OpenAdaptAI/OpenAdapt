"""Plain results for OpenAdapt run outcomes.

Every surface that tells a person or a calling AI agent how a run ended gets
the words from here, so no surface can say "nothing was written" when the
engine didn't prove it.

The rules live in ``openadapt_types/vocabulary/outcomes.json`` so that code
in other languages can apply the same table. This module applies them in
Python::

    from openadapt_flow.contracts import plain_outcome

    result = plain_outcome(
        transaction_outcome="RECONCILIATION_REQUIRED",
        execution_outcome="HALTED",
    )
    result.result            # PlainResultV1.CHECK_THE_RECORD
    result.label             # "Check the record"
    result.may_have_changed  # True
    result.safe_to_retry     # False
    result.next_action       # "A save may have gone through. Check the record ..."
    result.technical_code    # "transaction_outcome=RECONCILIATION_REQUIRED"

The safety rule: the plain result comes from the precise outcome
(``transaction_outcome`` or the Execute ``terminal_outcome``). A coarse value
such as ``HALTED`` on its own never proves that nothing changed, so it maps to
"Check the record". Only an outcome that proves no effect sets
``may_have_changed=False`` and ``safe_to_retry=True``. Anything the table
doesn't recognize, any disagreement between inputs, and any uncertain
delivery all fail closed to "Check the record".
"""

from __future__ import annotations

import re
from enum import Enum
from functools import lru_cache
from typing import Any, Literal, Mapping, NamedTuple

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    model_validator,
)

from openadapt_flow.contracts.execute import (
    ExecuteEvidenceReceiptV1,
    ExecuteStatusV1,
    ExecuteTerminalWebhookV1,
)
from openadapt_flow.contracts.vocabulary.lint import (
    MAY_CHANGE_RESULTS,
    NO_CHANGE_RESULTS,
    load_registry,
)

PLAIN_OUTCOME_SCHEMA = "openadapt.plain-outcome/v1"
PLAIN_PROGRESS_SCHEMA = "openadapt.plain-progress/v1"

# An outcome code: an enum value, never free text. Anything else is treated as
# unrecognized and kept out of ``exact`` so a stray string can't carry PHI.
_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_CODE_PATTERN = _CODE.pattern
_TECHNICAL_CODE_PATTERN = (
    r"^(transaction_outcome|execute_terminal_outcome|execute_state|execution_outcome"
    r"|agent_status|connector_status|clinic_tool_status|program_terminal_state|overlay_phase"
    r"|fallback)"
    r"=[A-Za-z][A-Za-z0-9_]{0,63}$"
)


class PlainResultV1(str, Enum):
    """How a run ended, in the words a person reads."""

    DONE = "done"
    STOPPED_BEFORE_CHANGE = "stopped_before_change"
    CHECK_THE_RECORD = "check_the_record"
    FINISHED_NOT_CHECKED = "finished_not_checked"
    DID_NOT_FINISH = "did_not_finish"
    REFUSED = "refused"


class PlainProofV1(str, Enum):
    """What backs the result. Separate from the result itself.

    ``sealed`` is a signed OpenAdapt receipt whose signature the caller
    checked. ``local`` is a run report on the computer that ran it. ``none``
    means only the status value backs the result.
    """

    SEALED = "sealed"
    LOCAL = "local"
    NONE = "none"


class OutcomeSourceV1(str, Enum):
    """Where an outcome value came from, in precedence order."""

    TRANSACTION_OUTCOME = "transaction_outcome"
    EXECUTE_TERMINAL_OUTCOME = "execute_terminal_outcome"
    EXECUTE_STATE = "execute_state"
    EXECUTION_OUTCOME = "execution_outcome"
    AGENT_STATUS = "agent_status"
    CONNECTOR_STATUS = "connector_status"
    CLINIC_TOOL_STATUS = "clinic_tool_status"
    PROGRAM_TERMINAL_STATE = "program_terminal_state"
    OVERLAY_PHASE = "overlay_phase"


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlainOutcomeInputsV1(_StrictContract):
    """The exact values a plain result was derived from.

    Surfaces show these one level deeper, for example in a Technical details
    disclosure, next to the plain result.
    """

    transaction_outcome: StrictStr | None = Field(default=None, pattern=_CODE_PATTERN)
    execute_terminal_outcome: StrictStr | None = Field(
        default=None, pattern=_CODE_PATTERN
    )
    execute_state: StrictStr | None = Field(default=None, pattern=_CODE_PATTERN)
    execution_outcome: StrictStr | None = Field(default=None, pattern=_CODE_PATTERN)
    agent_status: StrictStr | None = Field(default=None, pattern=_CODE_PATTERN)
    connector_status: StrictStr | None = Field(default=None, pattern=_CODE_PATTERN)
    clinic_tool_status: StrictStr | None = Field(default=None, pattern=_CODE_PATTERN)
    program_terminal_state: StrictStr | None = Field(
        default=None, pattern=_CODE_PATTERN
    )
    overlay_phase: StrictStr | None = Field(default=None, pattern=_CODE_PATTERN)
    dispatched: StrictBool | None = None
    delivery_uncertain: StrictBool | None = None


class PlainOutcomeV1(_StrictContract):
    """One run's result in plain words, with the exact values behind it.

    The model refuses any combination that breaks the safety rule, so a
    hand-built or deserialized result can't claim more than the table allows.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "x-openadapt-safety-rule": (
                "Only an outcome that proves no effect sets may_have_changed=false and "
                "safe_to_retry=true. A coarse HALTED alone never does."
            ),
        },
    )

    schema_version: Literal[PLAIN_OUTCOME_SCHEMA] = PLAIN_OUTCOME_SCHEMA
    result: PlainResultV1
    label: StrictStr = Field(min_length=1, max_length=64)
    may_have_changed: StrictBool
    safe_to_retry: StrictBool
    proof: PlainProofV1
    next_action: StrictStr = Field(min_length=1, max_length=300)
    technical_code: StrictStr = Field(pattern=_TECHNICAL_CODE_PATTERN)
    exact: PlainOutcomeInputsV1

    @model_validator(mode="after")
    def _keep_the_safety_rule(self) -> "PlainOutcomeV1":
        vocabulary, table = _registry()
        result = self.result.value
        expected = table["results"][result]["label"]
        if self.label != expected:
            raise ValueError(f"{result} is labeled {expected!r}, not {self.label!r}")
        if self.safe_to_retry and self.may_have_changed:
            raise ValueError("safe_to_retry needs may_have_changed=false")
        if result in NO_CHANGE_RESULTS and self.may_have_changed:
            raise ValueError(
                f"{result} says nothing changed, so may_have_changed must be false"
            )
        if result in MAY_CHANGE_RESULTS and (
            not self.may_have_changed or self.safe_to_retry
        ):
            raise ValueError(
                f"{result} must keep may_have_changed=true and safe_to_retry=false"
            )
        if self.may_have_changed:
            for pattern in vocabulary["no_change_claims"]["claim_patterns"]:
                match = re.search(pattern, self.next_action, re.IGNORECASE)
                if match:
                    raise ValueError(
                        f"next_action says {match.group(0)!r}, but something may have changed"
                    )
        if (
            self.proof is PlainProofV1.SEALED
            and self.exact.execute_terminal_outcome is None
        ):
            raise ValueError(
                "a signed receipt always carries an execute_terminal_outcome"
            )
        # The result must be the one the table gives for these exact inputs,
        # so a hand-built result can't read a bare HALTED as "nothing changed"
        # or call an unchecked run done. A fallback only adds caution, so it
        # stands for any inputs.
        source, _, value = self.technical_code.partition("=")
        if source == "fallback":
            row = table["fallbacks"].get(value)
            if row is None:
                raise ValueError(f"{self.technical_code} names no fallback")
        else:
            decision = _decide(
                table,
                {
                    name.value: getattr(self.exact, name.value)
                    for name in OutcomeSourceV1
                },
                dispatched=self.exact.dispatched,
                delivery_uncertain=self.exact.delivery_uncertain,
            )
            if decision.row is None or (decision.source, decision.value) != (
                source,
                value,
            ):
                raise ValueError(
                    f"technical_code {self.technical_code} isn't what the exact inputs decide"
                )
            row = decision.row
        if (row["result"], row["may_have_changed"], row["safe_to_retry"]) != (
            result,
            self.may_have_changed,
            self.safe_to_retry,
        ):
            raise ValueError(
                f"{self.technical_code} reads as {row['result']} with "
                f"may_have_changed={row['may_have_changed']} and "
                f"safe_to_retry={row['safe_to_retry']}"
            )
        return self


class PlainProgressV1(_StrictContract):
    """A run that hasn't ended yet. There's no result to show."""

    schema_version: Literal[PLAIN_PROGRESS_SCHEMA] = PLAIN_PROGRESS_SCHEMA
    state: Literal["queued", "running", "decision_required"]
    label: StrictStr = Field(min_length=1, max_length=64)
    next_action: StrictStr = Field(min_length=1, max_length=300)
    safe_to_retry: Literal[False] = False
    technical_code: StrictStr = Field(pattern=_TECHNICAL_CODE_PATTERN)

    @model_validator(mode="after")
    def _match_the_table(self) -> "PlainProgressV1":
        spec = _registry()[1]["in_progress"]["execute_state"].get(self.state)
        if spec is None or self.label != spec["label"]:
            raise ValueError(f"{self.state} is not labeled {self.label!r}")
        return self


class PlainOutcomeInProgress(ValueError):
    """Raised by :func:`plain_outcome` when the run hasn't ended yet."""

    def __init__(self, progress: PlainProgressV1) -> None:
        super().__init__(
            f"the run is still in progress ({progress.state}), so it has no result yet"
        )
        self.progress = progress


@lru_cache(maxsize=1)
def _registry() -> tuple[dict[str, Any], dict[str, Any]]:
    return load_registry()


class _Unrecognized:
    """A value that isn't an outcome code at all."""


_MALFORMED = _Unrecognized()


def _normalize(value: object) -> str | _Unrecognized | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, str) and _CODE.fullmatch(value):
        return value
    return _MALFORMED


def _progress_state(table: Mapping[str, Any], source: str, value: str) -> str:
    """The Execute progress state that an in-progress value reads as.

    Execute states name themselves. Any other source's in-progress entry
    names the Execute state it reads as, so every surface shows the same
    three progress labels.
    """

    if source == OutcomeSourceV1.EXECUTE_STATE.value:
        return value
    return table["in_progress"][source][value]["state"]


def _flag(name: str, value: object) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise TypeError(
            f"{name} must be True, False, or None, not {type(value).__name__}"
        )
    return value


class _Decision(NamedTuple):
    """What the table decides for one set of inputs.

    ``row`` is the selected row, or ``None`` while the run is in progress.
    ``source`` and ``value`` name the deciding input, or ``fallback`` and the
    fallback's name.
    """

    row: Mapping[str, Any] | None
    source: str
    value: str


def _decide(
    table: Mapping[str, Any],
    raw: Mapping[str, str | _Unrecognized | None],
    *,
    dispatched: bool | None,
    delivery_uncertain: bool | None,
) -> _Decision:
    """Apply ``merge_rules`` from outcomes.json to normalized inputs."""

    def fallback(name: str) -> _Decision:
        return _Decision(table["fallbacks"][name], "fallback", name)

    def settled(decision: _Decision) -> _Decision:
        # Rule 9.
        if (
            delivery_uncertain is True
            and decision.row is not None
            and decision.row["result"] != PlainResultV1.CHECK_THE_RECORD.value
        ):
            return fallback("delivery_uncertain")
        return decision

    # Rule 1: values that aren't outcomes add nothing.
    ignored = table.get("not_an_outcome", {})
    present = {
        source: value
        for source, value in raw.items()
        if value is not None and value not in ignored.get(source, [])
    }
    # Rule 10.
    if not present:
        return settled(fallback("missing"))
    # Rule 2: the first tier that still has an input decides.
    tier = next(
        tier for tier in table["tiers"] if any(source in present for source in tier)
    )

    rows: list[tuple[str, str, Mapping[str, Any]]] = []
    progress: list[tuple[str, str]] = []
    for source in tier:
        value = present.get(source)
        if value is None:
            continue
        # Rule 3.
        if not isinstance(value, str):
            return settled(fallback("unrecognized"))
        if value in table.get("in_progress", {}).get(source, {}):
            progress.append((source, value))
            continue
        row = table["sources"][source]["values"].get(value)
        if row is None:
            return settled(fallback("unrecognized"))
        # Rule 5.
        if dispatched is False and "if_not_dispatched" in row:
            row = row["if_not_dispatched"]
        rows.append((source, value, row))

    # Rule 4: the coarse outcome must be one the transaction outcome allows.
    transaction = table["sources"]["transaction_outcome"]["values"].get(
        present.get("transaction_outcome")  # type: ignore[arg-type]
    )
    coarse = present.get("execution_outcome")
    if transaction is not None and coarse is not None:
        if coarse not in transaction["execution_outcomes"]:
            return settled(fallback("conflict"))

    # Rule 6.
    if progress:
        if rows or len(progress) > 1:
            return settled(fallback("conflict"))
        source, value = progress[0]
        return _Decision(None, source, value)

    # Rules 7 and 8.
    if len({row["result"] for _, _, row in rows}) == 1:
        source, value, row = next(
            (hit for hit in rows if hit[2]["may_have_changed"]),
            next((hit for hit in rows if not hit[2]["safe_to_retry"]), rows[0]),
        )
    elif all(not row["may_have_changed"] for _, _, row in rows):
        source, value, row = rows[0]
    else:
        return settled(fallback("conflict"))
    return settled(_Decision(row, source, value))


def _resolve(
    values: Mapping[str, object],
    *,
    dispatched: object = None,
    delivery_uncertain: object = None,
    proof: PlainProofV1 | str | None = None,
) -> PlainOutcomeV1 | PlainProgressV1:
    """Apply ``merge_rules`` from outcomes.json to one set of inputs."""

    table = _registry()[1]
    dispatched = _flag("dispatched", dispatched)
    delivery_uncertain = _flag("delivery_uncertain", delivery_uncertain)
    proof_level = PlainProofV1.NONE if proof is None else PlainProofV1(proof)
    unknown = set(values) - {source.value for source in OutcomeSourceV1}
    if unknown:
        raise TypeError(f"unknown outcome sources: {sorted(unknown)}")

    raw = {source: _normalize(value) for source, value in values.items()}
    exact = PlainOutcomeInputsV1(
        **{source: value for source, value in raw.items() if isinstance(value, str)},
        dispatched=dispatched,
        delivery_uncertain=delivery_uncertain,
    )
    if proof_level is PlainProofV1.SEALED and exact.execute_terminal_outcome is None:
        raise ValueError(
            "proof=sealed needs an execute_terminal_outcome, because a signed receipt carries one"
        )

    decision = _decide(
        table, raw, dispatched=dispatched, delivery_uncertain=delivery_uncertain
    )
    if decision.row is None:
        state = _progress_state(table, decision.source, decision.value)
        spec = table["in_progress"]["execute_state"][state]
        return PlainProgressV1(
            state=state,
            label=spec["label"],
            next_action=spec["next_action"],
            technical_code=f"{decision.source}={decision.value}",
        )
    row = decision.row
    return PlainOutcomeV1(
        result=row["result"],
        label=table["results"][row["result"]]["label"],
        may_have_changed=row["may_have_changed"],
        safe_to_retry=row["safe_to_retry"],
        proof=proof_level,
        next_action=row["next_action"],
        technical_code=f"{decision.source}={decision.value}",
        exact=exact,
    )


def plain_outcome(
    *,
    transaction_outcome: str | Enum | None = None,
    execute_terminal_outcome: str | Enum | None = None,
    execute_state: str | Enum | None = None,
    execution_outcome: str | Enum | None = None,
    agent_status: str | Enum | None = None,
    connector_status: str | Enum | None = None,
    clinic_tool_status: str | Enum | None = None,
    program_terminal_state: str | Enum | None = None,
    overlay_phase: str | Enum | None = None,
    dispatched: bool | None = None,
    delivery_uncertain: bool | None = None,
    proof: PlainProofV1 | str | None = None,
) -> PlainOutcomeV1:
    """The plain result for one run, from whatever outcome values you have.

    Pass every value you have. The precise ones (``transaction_outcome``,
    ``execute_terminal_outcome``) decide when present; coarse ones decide only
    when nothing more precise came back, and then never claim that nothing
    changed. ``overlay_phase`` is the phase on a control overlay frame; its
    end phases mirror the coarse ``execution_outcome``.

    ``dispatched`` is ``False`` only when you know the request never reached
    the app (for example, a timeout before the run started). ``True`` and
    ``None`` change nothing. ``delivery_uncertain=True`` turns any result into
    "Check the record". ``proof`` says what backs the result and defaults to
    ``none``.

    Raises :class:`PlainOutcomeInProgress` when the deciding input says the
    run hasn't ended: an Execute state such as queued, running, or waiting
    for a person, or an overlay phase such as executing or paused.
    """

    resolved = _resolve(
        {
            "transaction_outcome": transaction_outcome,
            "execute_terminal_outcome": execute_terminal_outcome,
            "execute_state": execute_state,
            "execution_outcome": execution_outcome,
            "agent_status": agent_status,
            "connector_status": connector_status,
            "clinic_tool_status": clinic_tool_status,
            "program_terminal_state": program_terminal_state,
            "overlay_phase": overlay_phase,
        },
        dispatched=dispatched,
        delivery_uncertain=delivery_uncertain,
        proof=proof,
    )
    if isinstance(resolved, PlainProgressV1):
        raise PlainOutcomeInProgress(resolved)
    return resolved


def plain_outcome_for_run_report(report: Mapping[str, Any]) -> PlainOutcomeV1:
    """The plain result for a Flow ``report.json`` read from the computer that ran it."""

    if not isinstance(report, Mapping):
        raise TypeError("report must be the parsed report.json object")
    return plain_outcome(
        transaction_outcome=report.get("transaction_outcome"),
        execution_outcome=report.get("execution_outcome"),
        program_terminal_state=report.get("terminal_outcome"),
        proof=PlainProofV1.LOCAL,
    )


def plain_outcome_for_execute_status(
    status: ExecuteStatusV1,
    *,
    proof: PlainProofV1 | str | None = None,
) -> PlainOutcomeV1 | PlainProgressV1:
    """The plain result, or the progress, for an Execute status."""

    return _resolve(
        {
            "execute_state": status.state,
            "execute_terminal_outcome": status.terminal_outcome,
        },
        proof=proof,
    )


def plain_outcome_for_execute_receipt(
    receipt: ExecuteEvidenceReceiptV1,
    *,
    proof: PlainProofV1 | str,
) -> PlainOutcomeV1:
    """The plain result for an Execute receipt.

    Pass ``proof="sealed"`` only after you've checked where the receipt came
    from, for example with :func:`plain_outcome_for_execute_webhook`.
    """

    return plain_outcome(
        execute_terminal_outcome=receipt.outcome,
        delivery_uncertain=receipt.delivery_uncertain,
        proof=proof,
    )


def plain_outcome_for_execute_webhook(
    webhook: ExecuteTerminalWebhookV1,
    *,
    key: bytes,
) -> PlainOutcomeV1:
    """The plain result for a terminal Execute webhook, after checking its signature."""

    if not webhook.verify_hmac(key):
        raise ValueError(
            "the webhook signature doesn't verify, so its receipt can't back a result"
        )
    return plain_outcome_for_execute_receipt(webhook.receipt, proof=PlainProofV1.SEALED)


__all__ = [
    "PLAIN_OUTCOME_SCHEMA",
    "PLAIN_PROGRESS_SCHEMA",
    "OutcomeSourceV1",
    "PlainOutcomeInProgress",
    "PlainOutcomeInputsV1",
    "PlainOutcomeV1",
    "PlainProgressV1",
    "PlainProofV1",
    "PlainResultV1",
    "plain_outcome",
    "plain_outcome_for_execute_receipt",
    "plain_outcome_for_execute_status",
    "plain_outcome_for_execute_webhook",
    "plain_outcome_for_run_report",
]
