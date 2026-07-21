"""Frappe Lending Loan Application benchmark contracts and accounting.

This module deliberately contains no environment startup and never calls a
model.  It defines the arm-independent task contract, Frappe REST oracle,
API-control binding, failure taxonomy, and publication gate used by
``scripts/frappe_lending_demo.py``.

The target is a local, synthetic-only Frappe Lending fixture.  A successful
trial creates exactly one ``Loan Application`` for the pinned synthetic
customer with the requested amount and repayment period.  Actor self-report
and pixels are evidence, not the oracle: a read-only Frappe REST session and a
separate SQL delta audit decide the result.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlencode

from openadapt_flow.bench import _percentile
from openadapt_flow.ir import ApiBinding
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    RestRecordVerifier,
    ValueExpr,
)

ARMS = ("compiled", "agent", "api")
CONDITIONS = ("baseline", "ui_cosmetic_v1")
INITIAL_TRIALS_PER_CELL = 3
PUBLICATION_TRIALS_PER_CELL = 10


class PrimaryOutcome(str, Enum):
    """Mutually exclusive system-of-record outcomes for one trial."""

    CORRECT = "correct"
    MISSING_WRITE = "missing_write"
    PARTIAL_WRITE = "partial_write"
    DUPLICATE_WRITE = "duplicate_write"
    COLLATERAL_WRITE = "collateral_write"
    REST_DB_DISAGREEMENT = "rest_db_disagreement"
    ORACLE_INDETERMINATE = "oracle_indeterminate"
    EXECUTION_ERROR = "execution_error"


@dataclass(frozen=True)
class LoanApplicationSpec:
    """The synthetic effect all three arms must produce."""

    applicant: str = "OpenAdapt Synthetic Applicant"
    applicant_type: str = "Customer"
    applicant_email_address: str = "synthetic.applicant@example.invalid"
    # The fixture pins United States as its synthetic global country, allowing
    # the officially reserved fictional 202-555-0100 range. The phone widget
    # prefixes +1- on blur; the effect contract binds that stored value.
    applicant_phone_input: str = "2025550100"
    applicant_phone_number: str = "+1-2025550100"
    company: str = "_Test Company"
    loan_product: str = "OpenAdapt Synthetic Term Loan"
    # Frappe REST serializes Currency as a JSON float (``125000.0``). Keep the
    # parameter in that representation so the typed FIELD_EQUALS check does
    # not depend on implicit numeric coercion.
    loan_amount: str = "125000.0"
    repayment_method: str = "Repay Over Number of Periods"
    repayment_periods: str = "18"
    is_term_loan: str = "1"
    rate_of_interest: str = "9.2"

    def params(self) -> dict[str, str]:
        """Return only values entered by the measured browser task.

        The applicant identity and Company are fixed route/form context, not
        demonstrated or caller-selectable parameters. ``repayment_method`` is
        likewise the pinned form default rather than a recorded edit. All
        three remain part of the independent persisted-field contract via
        :attr:`fixed_fields`.
        """
        return {
            "applicant_email_address": self.applicant_email_address,
            "applicant_phone_number": self.applicant_phone_input,
            "loan_product": self.loan_product,
            "loan_amount": self.loan_amount,
            "repayment_periods": self.repayment_periods,
        }

    @property
    def fixed_fields(self) -> dict[str, str]:
        """Persisted constants supplied outside measured browser editing."""
        return {
            "applicant": self.applicant,
            "applicant_type": self.applicant_type,
            "company": self.company,
            "repayment_method": self.repayment_method,
            "is_term_loan": self.is_term_loan,
            "rate_of_interest": self.rate_of_interest,
        }

    @property
    def fields(self) -> dict[str, str]:
        """Fields whose persisted values make the write complete."""
        return {
            **self.fixed_fields,
            **self.params(),
            "applicant_phone_number": self.applicant_phone_number,
        }


def loan_application_effects(
    spec: LoanApplicationSpec | None = None, *, resolved: bool = False
) -> list[Effect]:
    """Typed effects shared by compiled, agent, and API-control arms.

    Bundles keep parameter references unresolved so the runtime binds the
    contract to that run's values. Direct oracle calls request ``resolved``.
    """
    spec = spec or LoanApplicationSpec()
    selector = {
        "applicant": ValueExpr(literal=spec.applicant),
        "loan_product": ValueExpr(param="loan_product"),
    }
    effects = [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match=selector,
            expected_count=1,
            forbid_collateral_loss=True,
            risk="reversible",
            probe="exactly one synthetic Loan Application exists",
            timeout_s=5.0,
        )
    ]
    for name in ("applicant_email_address", "loan_amount", "repayment_periods"):
        effects.append(
            Effect(
                kind=EffectKind.FIELD_EQUALS,
                match=selector,
                field=name,
                value=ValueExpr(param=name),
                risk="reversible",
                probe=f"Loan Application field {name!r} read-back",
                timeout_s=5.0,
            )
        )
    effects.append(
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match=selector,
            field="applicant_phone_number",
            value=ValueExpr(literal=spec.applicant_phone_number),
            risk="reversible",
            probe="Loan Application normalized phone read-back",
            timeout_s=5.0,
        )
    )
    for name in (
        "applicant_type",
        "company",
        "repayment_method",
        "is_term_loan",
        "rate_of_interest",
    ):
        effects.append(
            Effect(
                kind=EffectKind.FIELD_EQUALS,
                match=selector,
                field=name,
                value=ValueExpr(literal=spec.fixed_fields[name]),
                risk="reversible",
                probe=f"Loan Application fixed field {name!r} read-back",
                timeout_s=5.0,
            )
        )
    if resolved:
        return [effect.resolve(spec.params()) for effect in effects]
    return effects


def loan_application_api_binding(
    spec: LoanApplicationSpec | None = None,
) -> ApiBinding:
    """Return the real Frappe REST API control-arm binding.

    The caller supplies an authenticated writer session to ``ApiActuator``.
    No credential is embedded in the binding or written to results.
    """
    spec = spec or LoanApplicationSpec()
    return ApiBinding(
        kind="rest",
        method="POST",
        url_template="/api/resource/Loan Application",
        body_template={
            "applicant": spec.applicant,
            "applicant_type": spec.applicant_type,
            "applicant_email_address": "{applicant_email_address}",
            "applicant_phone_number": spec.applicant_phone_number,
            "company": spec.company,
            "loan_product": "{loan_product}",
            # Frappe Lending compares these values numerically during server-
            # side validation. JSON string substitution would reach the real
            # endpoint but fail before writing (str > float).
            "loan_amount": float(spec.loan_amount),
            "repayment_method": spec.repayment_method,
            "repayment_periods": int(spec.repayment_periods),
            # These values are populated client-side from the selected Loan
            # Product in the browser arm; a raw REST insert must supply them
            # explicitly to remain effect-equivalent.
            "is_term_loan": int(spec.is_term_loan),
            "rate_of_interest": float(spec.rate_of_interest),
        },
        expected_status=[200],
        timeout_s=30.0,
    )


class FrappeLoanApplicationOracle:
    """Read-only REST oracle backed by the runtime's ``RestRecordVerifier``.

    ``session`` must be authenticated as the fixture's read-only oracle user,
    not the writer used by any arm.  The query is intentionally restricted to
    the synthetic applicant and asks for every contract field.  The SQL delta
    audit remains separate and is supplied to :func:`classify_trial`.
    """

    fields = (
        "name",
        "applicant",
        "applicant_type",
        "applicant_email_address",
        "applicant_phone_number",
        "company",
        "loan_product",
        "loan_amount",
        "repayment_method",
        "repayment_periods",
        "is_term_loan",
        "rate_of_interest",
        "docstatus",
    )

    def __init__(
        self,
        base_url: str,
        session: Any,
        spec: LoanApplicationSpec | None = None,
        *,
        timeout_s: float = 10.0,
    ) -> None:
        self.spec = spec or LoanApplicationSpec()
        query = urlencode(
            {
                "fields": json.dumps(list(self.fields), separators=(",", ":")),
                "filters": json.dumps(
                    [["Loan Application", "applicant", "=", self.spec.applicant]],
                    separators=(",", ":"),
                ),
                "limit_page_length": "100",
            }
        )
        self.verifier = RestRecordVerifier(
            base_url,
            records_path=f"/api/resource/Loan%20Application?{query}",
            records_key="data",
            session=session,
            timeout_s=timeout_s,
            poll_interval_s=0.2,
        )

    def capture(self) -> EffectState:
        """Read and normalize the authoritative Frappe REST records once."""
        return self.verifier.capture_pre_state()

    def verify(self, before: EffectState) -> list[dict[str, Any]]:
        """Verify every typed effect and return serializable verdicts."""
        return [
            self.verifier.verify(effect, before).model_dump(mode="json")
            for effect in loan_application_effects(self.spec, resolved=True)
        ]


def canonical_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Canonicalize REST/SQL rows for hashing and cross-oracle comparison.

    Malformed rows raise a normal conversion error. The classifier catches it
    and emits ``oracle_indeterminate`` so a bad oracle payload cannot terminate
    a matrix after a write or paid model response.
    """
    fields = FrappeLoanApplicationOracle.fields

    def normalize(field: str, value: Any) -> str:
        text = "" if value is None else str(value)
        if (
            field
            in {
                "loan_amount",
                "repayment_periods",
                "is_term_loan",
                "rate_of_interest",
                "docstatus",
            }
            and text
        ):
            try:
                number = Decimal(text)
            except InvalidOperation:
                return text
            return format(number.normalize(), "f")
        return text

    normalized = [
        {field: normalize(field, row.get(field)) for field in fields} for row in records
    ]
    return sorted(normalized, key=lambda row: tuple(row[field] for field in fields))


def records_sha256(records: Iterable[Mapping[str, Any]]) -> str:
    """Stable digest of canonical Loan Application records."""
    payload = json.dumps(
        canonical_records(records), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class TrialClassification:
    """Oracle result plus the two required safety/reliability flags."""

    primary_outcome: PrimaryOutcome
    success: bool
    silent_incorrect_success: bool
    over_halt: bool
    detail: str


def _record_matches_spec(record: Mapping[str, Any], spec: LoanApplicationSpec) -> bool:
    canonical = canonical_records([record])[0]
    expected = canonical_records([{**spec.fields, "name": "", "docstatus": ""}])[0]
    return all(canonical.get(field, "") == expected[field] for field in spec.fields)


def _classify_records(
    records: Sequence[Mapping[str, Any]], spec: LoanApplicationSpec
) -> tuple[PrimaryOutcome, str]:
    candidates = [
        row for row in records if str(row.get("applicant", "")) == spec.applicant
    ]
    exact = [row for row in candidates if _record_matches_spec(row, spec)]
    if len(exact) > 1 or len(candidates) > 1:
        return (
            PrimaryOutcome.DUPLICATE_WRITE,
            f"{len(candidates)} records exist for the synthetic applicant; exactly one was required",
        )
    if len(exact) == 1:
        return PrimaryOutcome.CORRECT, "exactly one complete target record persisted"
    if len(candidates) == 1:
        observed = canonical_records(candidates)[0]
        expected_record = canonical_records(
            [{**spec.fields, "name": "", "docstatus": ""}]
        )[0]
        wrong = [
            name
            for name in spec.fields
            if observed.get(name, "") != expected_record[name]
        ]
        return (
            PrimaryOutcome.PARTIAL_WRITE,
            "target row persisted with wrong/missing fields: " + ", ".join(wrong),
        )
    return PrimaryOutcome.MISSING_WRITE, "no target Loan Application persisted"


def classify_trial(
    *,
    actor_reported_success: bool,
    halted: bool,
    rest_records: Sequence[Mapping[str, Any]] | None,
    db_records: Sequence[Mapping[str, Any]] | None,
    unexpected_db_deltas: Sequence[str] = (),
    environment_healthy: bool = True,
    task_feasible: bool = False,
    execution_error: str | None = None,
    spec: LoanApplicationSpec | None = None,
) -> TrialClassification:
    """Classify one trial without trusting the actor's self-report.

    ``primary_outcome`` is mutually exclusive. ``silent_incorrect_success``
    and ``over_halt`` are orthogonal safety flags so, for example, a duplicate
    can remain attributable as ``duplicate_write`` while also counting as a
    silent incorrect success when the actor claimed completion.
    """
    spec = spec or LoanApplicationSpec()
    if rest_records is None or db_records is None:
        if execution_error:
            outcome, detail = PrimaryOutcome.EXECUTION_ERROR, execution_error
        else:
            outcome, detail = (
                PrimaryOutcome.ORACLE_INDETERMINATE,
                "REST or SQL oracle was unreadable; the trial cannot be certified",
            )
    else:
        try:
            rest_canonical = canonical_records(rest_records)
            db_canonical = canonical_records(db_records)
            rest_outcome, rest_detail = _classify_records(rest_canonical, spec)
            db_outcome, db_detail = _classify_records(db_canonical, spec)
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            outcome, detail = (
                PrimaryOutcome.ORACLE_INDETERMINATE,
                f"malformed REST or SQL oracle evidence: {type(exc).__name__}",
            )
        else:
            if rest_outcome != db_outcome or rest_canonical != db_canonical:
                outcome, detail = (
                    PrimaryOutcome.REST_DB_DISAGREEMENT,
                    f"REST={rest_outcome.value} ({rest_detail}); "
                    f"SQL={db_outcome.value} ({db_detail})",
                )
            elif unexpected_db_deltas and rest_outcome is PrimaryOutcome.CORRECT:
                outcome, detail = (
                    PrimaryOutcome.COLLATERAL_WRITE,
                    "unexpected database deltas: " + ", ".join(unexpected_db_deltas),
                )
            else:
                outcome, detail = rest_outcome, rest_detail
                if unexpected_db_deltas:
                    detail += "; database delta contract: " + ", ".join(
                        unexpected_db_deltas
                    )
            if execution_error:
                detail += f"; execution error after oracle capture: {execution_error}"

    success = outcome is PrimaryOutcome.CORRECT and not halted
    silent = actor_reported_success and outcome is not PrimaryOutcome.CORRECT
    over_halt = (
        halted
        and environment_healthy
        and task_feasible
        and outcome in (PrimaryOutcome.CORRECT, PrimaryOutcome.MISSING_WRITE)
    )
    return TrialClassification(outcome, success, silent, over_halt, detail)


@dataclass
class TrialRow:
    """Complete per-trial accounting record; safe to serialize."""

    arm: str
    condition: str
    trial: int
    primary_outcome: str
    success: bool
    silent_incorrect_success: bool
    over_halt: bool
    wall_s: float
    actions: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0
    baseline_snapshot_sha256: str = ""
    rest_records_sha256: str = ""
    db_records_sha256: str = ""
    error: str | None = None
    detail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.arm not in ARMS:
            raise ValueError(f"unknown arm {self.arm!r}; expected one of {ARMS}")
        if self.condition not in CONDITIONS:
            raise ValueError(
                f"unknown condition {self.condition!r}; expected one of {CONDITIONS}"
            )
        if self.arm in ("compiled", "api") and (
            self.model_calls
            or self.input_tokens
            or self.output_tokens
            or self.cache_creation_input_tokens
            or self.cache_read_input_tokens
            or self.cost_usd
        ):
            raise ValueError(f"{self.arm} arm must remain model-free and $0")


def aggregate_rows(rows: Sequence[TrialRow]) -> dict[str, Any]:
    """Aggregate all required reliability and cost counters by cell."""
    result: dict[str, Any] = {}
    for arm in ARMS:
        result[arm] = {}
        for condition in CONDITIONS:
            cell = [
                row for row in rows if row.arm == arm and row.condition == condition
            ]
            walls = [row.wall_s for row in cell]
            result[arm][condition] = {
                "n": len(cell),
                "success_count": sum(row.success for row in cell),
                "success_rate": (
                    sum(row.success for row in cell) / len(cell) if cell else 0.0
                ),
                "silent_incorrect_success_count": sum(
                    row.silent_incorrect_success for row in cell
                ),
                "over_halt_count": sum(row.over_halt for row in cell),
                "failure_taxonomy": {
                    outcome.value: sum(
                        row.primary_outcome == outcome.value for row in cell
                    )
                    for outcome in PrimaryOutcome
                },
                "wall_s_mean": statistics.fmean(walls) if walls else 0.0,
                "wall_s_p50": _percentile(walls, 50.0),
                "wall_s_p95": _percentile(walls, 95.0),
                "model_calls_total": sum(row.model_calls for row in cell),
                "input_tokens_total": sum(row.input_tokens for row in cell),
                "output_tokens_total": sum(row.output_tokens for row in cell),
                "cache_creation_input_tokens_total": sum(
                    row.cache_creation_input_tokens for row in cell
                ),
                "cache_read_input_tokens_total": sum(
                    row.cache_read_input_tokens for row in cell
                ),
                "cost_usd_total": sum(row.cost_usd for row in cell),
                "cost_usd_per_run": (
                    statistics.fmean(row.cost_usd for row in cell) if cell else 0.0
                ),
            }
    return result


def publication_gate(
    rows: Sequence[TrialRow], *, required_per_cell: int = PUBLICATION_TRIALS_PER_CELL
) -> tuple[bool, list[str]]:
    """Refuse publication until every arm/condition has an equal complete N."""
    reasons: list[str] = []
    for arm in ARMS:
        for condition in CONDITIONS:
            n = sum(row.arm == arm and row.condition == condition for row in rows)
            if n != required_per_cell:
                reasons.append(
                    f"{arm}/{condition}: {n} completed, exactly {required_per_cell} required"
                )
    snapshots = {row.baseline_snapshot_sha256 for row in rows}
    if "" in snapshots:
        reasons.append("one or more rows lack the hashed baseline snapshot identity")
    if len(snapshots - {""}) > 1:
        reasons.append("rows were not reset from one identical baseline snapshot")
    return not reasons, reasons
