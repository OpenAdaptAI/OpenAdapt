"""Matched local OpenEMR patient-registration benchmark contracts.

This module contains no environment startup and never calls a model.  It
defines the synthetic task, shared effect contract, read-only REST oracle,
direct-API control binding, record canonicalization, and outcome classifier.

The result row, aggregation, matrix, and publication-gate schema is imported
from :mod:`.frappe_lending` intentionally: the OpenEMR and Frappe runs must use
the exact same arms, conditions, trial gates, counters, and failure taxonomy.
Only their application-specific record contracts differ.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlencode

from openadapt_flow.benchmark.frappe_lending import (
    ARMS,
    CONDITIONS,
    INITIAL_TRIALS_PER_CELL,
    PUBLICATION_TRIALS_PER_CELL,
    PrimaryOutcome,
    TrialClassification,
    TrialRow,
    aggregate_rows,
    publication_gate,
)
from openadapt_flow.ir import ApiBinding
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    RestRecordVerifier,
    ValueExpr,
)


@dataclass(frozen=True)
class SyntheticPatientSpec:
    """The one synthetic patient every arm must create.

    Reserved example domains and the North American fictional 555-01xx range
    prevent the fixture from being mistaken for real patient information.
    """

    title: str = "Ms."
    fname: str = "OpenAdapt"
    lname: str = "LoanParity"
    dob: str = "1985-02-03"
    sex: str = "Female"
    # The pinned demographics form canonicalizes these fields to uppercase on
    # save. Type the canonical representation so browser and API arms share an
    # exact persisted effect rather than hiding normalization in the oracle.
    street: str = "101 SYNTHETIC WAY"
    city: str = "EXAMPLETON"
    # The persisted code and the native select's label are intentionally
    # distinct. Typing ``MA`` into the label selector can prefix-match Maine.
    state: str = "MA"
    state_label: str = "Massachusetts"
    postal_code: str = "02139"
    phone_home: str = "555-0107"
    email: str = "openadapt.loan-parity@example.invalid"
    country_code: str = "USA"

    def params(self) -> dict[str, str]:
        """Return exact demonstration/runtime parameters.

        ``state_label`` is the browser input. The persisted ``state`` code is
        fixed by the task contract instead of being typed into the select.
        """
        return {
            "title": self.title,
            "fname": self.fname,
            "lname": self.lname,
            "DOB": self.dob,
            "sex": self.sex,
            "street": self.street,
            "city": self.city,
            "state_label": self.state_label,
            "postal_code": self.postal_code,
            "phone_home": self.phone_home,
            "email": self.email,
            "country_code": self.country_code,
        }

    @property
    def fields(self) -> dict[str, str]:
        """Persisted fields whose equality makes the target write complete."""
        return {
            "title": self.title,
            "fname": self.fname,
            "lname": self.lname,
            "DOB": self.dob,
            "sex": self.sex,
            "street": self.street,
            "city": self.city,
            "state": self.state,
            "postal_code": self.postal_code,
            "phone_home": self.phone_home,
            "email": self.email,
            "country_code": self.country_code,
        }


def patient_effects(
    spec: SyntheticPatientSpec | None = None, *, resolved: bool = False
) -> list[Effect]:
    """Effects shared by compiled, agent, and direct-API arms.

    The selector deliberately uses both a reserved email and the synthetic
    family name.  A single coincidental field match cannot satisfy the write.
    """
    spec = spec or SyntheticPatientSpec()
    selector = {
        "email": ValueExpr(param="email"),
        "lname": ValueExpr(param="lname"),
    }
    effects = [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match=selector,
            expected_count=1,
            forbid_collateral_loss=True,
            risk="reversible",
            probe="exactly one synthetic OpenEMR patient exists",
            timeout_s=5.0,
        )
    ]
    for field in (
        "title",
        "fname",
        "DOB",
        "sex",
        "street",
        "city",
        "state",
        "postal_code",
        "phone_home",
        "country_code",
    ):
        value = (
            ValueExpr(literal=spec.state)
            if field == "state"
            else ValueExpr(param=field)
        )
        effects.append(
            Effect(
                kind=EffectKind.FIELD_EQUALS,
                match=selector,
                field=field,
                value=value,
                risk="reversible",
                probe=f"OpenEMR patient field {field!r} read-back",
                timeout_s=5.0,
            )
        )
    if resolved:
        return [effect.resolve(spec.params()) for effect in effects]
    return effects


def patient_api_binding() -> ApiBinding:
    """OpenEMR Standard REST control-arm binding for patient creation."""
    spec = SyntheticPatientSpec()
    return ApiBinding(
        kind="rest",
        method="POST",
        url_template="/apis/default/api/patient",
        body_template={
            field: spec.state if field == "state" else "{" + field + "}"
            for field in spec.fields
        },
        expected_status=[201],
        timeout_s=30.0,
    )


class OpenEMRPatientOracle:
    """Patient readback through a separately authenticated read-only client.

    The caller must supply a session whose bearer token has exactly
    ``openid api:oemr user/patient.rs``.  The fixture issues it from a client
    distinct from the direct-API writer client.  Direct SQL remains a second
    oracle and is supplied separately to :func:`classify_patient_trial`.
    """

    fields = (
        "id",
        "pid",
        "uuid",
        "title",
        "fname",
        "lname",
        "DOB",
        "sex",
        "street",
        "city",
        "state",
        "postal_code",
        "phone_home",
        "email",
        "country_code",
    )

    def __init__(
        self,
        base_url: str,
        session: Any,
        spec: SyntheticPatientSpec | None = None,
        *,
        timeout_s: float = 10.0,
    ) -> None:
        self.spec = spec or SyntheticPatientSpec()
        query = urlencode(
            {
                "lname": self.spec.lname,
                "email": self.spec.email,
                "_limit": "100",
            }
        )
        self.verifier = RestRecordVerifier(
            base_url.rstrip("/"),
            records_path=f"/apis/default/api/patient?{query}",
            records_key="data",
            session=session,
            timeout_s=timeout_s,
            poll_interval_s=0.2,
        )

    def capture(self) -> EffectState:
        """Fetch one authoritative REST state without mutating the target."""
        return self.verifier.capture_pre_state()

    def verify(self, before: EffectState) -> list[dict[str, Any]]:
        """Evaluate the complete typed effect contract."""
        return [
            self.verifier.verify(effect, before).model_dump(mode="json")
            for effect in patient_effects(self.spec, resolved=True)
        ]


def _normalize_uuid(value: Any) -> str:
    return str(value or "").lower().replace("-", "")


def canonical_patient_records(
    records: Iterable[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Normalize Standard REST and direct-SQL patient rows identically.

    Malformed identifiers raise ``ValueError``; the trial classifier catches
    that and returns ``oracle_indeterminate``.  A bad oracle payload must never
    terminate a matrix after a write or after paid usage has occurred.
    """
    normalized: list[dict[str, str]] = []
    for row in records:
        item: dict[str, str] = {}
        for field in OpenEMRPatientOracle.fields:
            value = row.get(field)
            if field == "uuid":
                item[field] = _normalize_uuid(value)
            elif field in {"id", "pid"}:
                item[field] = "" if value is None else str(int(value))
            else:
                item[field] = "" if value is None else str(value).strip()
        normalized.append(item)
    return sorted(
        normalized,
        key=lambda item: tuple(item[field] for field in OpenEMRPatientOracle.fields),
    )


def patient_records_sha256(records: Iterable[Mapping[str, Any]]) -> str:
    """Stable digest for oracle evidence without writing field values to logs."""
    payload = json.dumps(
        canonical_patient_records(records), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _record_matches_spec(record: Mapping[str, Any], spec: SyntheticPatientSpec) -> bool:
    observed = canonical_patient_records([record])[0]
    expected = canonical_patient_records(
        [{**spec.fields, "id": 0, "pid": 0, "uuid": ""}]
    )[0]
    return all(observed[field] == expected[field] for field in spec.fields)


def _classify_records(
    records: Sequence[Mapping[str, Any]], spec: SyntheticPatientSpec
) -> tuple[PrimaryOutcome, str]:
    candidates = [
        row
        for row in records
        if str(row.get("lname", "")) == spec.lname
        and str(row.get("email", "")) == spec.email
    ]
    exact = [row for row in candidates if _record_matches_spec(row, spec)]
    if len(exact) > 1 or len(candidates) > 1:
        return (
            PrimaryOutcome.DUPLICATE_WRITE,
            f"{len(candidates)} synthetic patient rows exist; exactly one was required",
        )
    if len(exact) == 1:
        return (
            PrimaryOutcome.CORRECT,
            "exactly one complete synthetic patient persisted",
        )
    if len(candidates) == 1:
        observed = canonical_patient_records(candidates)[0]
        expected = canonical_patient_records(
            [{**spec.fields, "id": 0, "pid": 0, "uuid": ""}]
        )[0]
        wrong = [
            field for field in spec.fields if observed.get(field, "") != expected[field]
        ]
        return (
            PrimaryOutcome.PARTIAL_WRITE,
            "patient row persisted with wrong/missing fields: " + ", ".join(wrong),
        )
    return PrimaryOutcome.MISSING_WRITE, "no target synthetic patient persisted"


def classify_patient_trial(
    *,
    actor_reported_success: bool,
    halted: bool,
    rest_records: Sequence[Mapping[str, Any]] | None,
    db_records: Sequence[Mapping[str, Any]] | None,
    unexpected_db_deltas: Sequence[str] = (),
    environment_healthy: bool = True,
    task_feasible: bool = False,
    execution_error: str | None = None,
    spec: SyntheticPatientSpec | None = None,
) -> TrialClassification:
    """Classify a trial from independent evidence, never actor self-report."""
    spec = spec or SyntheticPatientSpec()
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
            rest_canonical = canonical_patient_records(rest_records)
            db_canonical = canonical_patient_records(db_records)
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


__all__ = [
    "ARMS",
    "CONDITIONS",
    "INITIAL_TRIALS_PER_CELL",
    "PUBLICATION_TRIALS_PER_CELL",
    "PrimaryOutcome",
    "TrialClassification",
    "TrialRow",
    "aggregate_rows",
    "publication_gate",
    "SyntheticPatientSpec",
    "OpenEMRPatientOracle",
    "canonical_patient_records",
    "classify_patient_trial",
    "patient_api_binding",
    "patient_effects",
    "patient_records_sha256",
]
