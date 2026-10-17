"""Frappe Lending task family — containerized ERP web substrate (needs Docker).

These tasks target the pinned ``frappe_lending`` environment (Frappe/ERPNext +
Lending v16; ``benchmark/environments`` registry). The independent oracle reads
the true effect off-screen from Frappe's MariaDB system of record — the
`` `tabLoan Application` `` row-state — via a read-only SELECT, or Frappe's REST
authenticated as the read-only oracle user the registry pins
(``openadapt.oracle@example.invalid``; a custom read-only Loan Application
permission, NOT the UI/API writer the agent drives).

Status: AUTHORED + STATICALLY WIRED, NOT YET RUN. Frappe bring-up builds a
custom image from pinned upstreams and needs Docker + ~40 GiB, so these are NOT
executed here. Each oracle's read recipe is validated statically (the SQL is a
single read-only SELECT accepted by ``assert_read_only_sql``; its bound params
exist; the effect selector/field reference real params), and every task carries
``adversarially_audited=False`` until a container red-team pass.

``oracle.config["query"]`` + ``["query_params"]`` is what the multi-baseline
runner materializes into a
:class:`~openadapt_flow.benchmark.effectbench.SqlRecordVerifier` (read-only role,
DB-API-bound params). The loan-application docname has a space
(`` `tabLoan Application` ``), so the table is back-tick quoted in the SELECT.
"""

from __future__ import annotations

from benchmark.effectbench.task_pack._authoring import (
    PARAM_NOTE,
    PARAM_TARGET,
    field_equals_effect,
    oracle_spec,
    param,
    record_written_effect,
)
from benchmark.effectbench.task_pack.openemr_tasks import ContainerTask
from openadapt_flow.benchmark.effectbench import (
    DivergenceCategory as DC,
)
from openadapt_flow.benchmark.effectbench import (
    Effect,
    Substrate,
    TaskSpec,
)
from openadapt_flow.benchmark.effectbench.schema import OracleChannel

ENVIRONMENT = "frappe_lending"

# Read-only SELECTs against the Frappe system of record. ``:target_id`` binds a
# trial-unique applicant reference; ``loan_amount`` / ``status`` are the
# consequential fields. The docname carries a space, hence the back-ticks.
SQL_LOAN_BY_APPLICANT = (
    "SELECT name, applicant, loan_amount, status, workflow_state "
    "FROM `tabLoan Application` WHERE applicant = :target_id"
)


def _sql_oracle(description: str, *, refusal: bool = False):
    return oracle_spec(
        OracleChannel.SQL,
        description=description,
        config={
            "environment": ENVIRONMENT,
            "query": SQL_LOAN_BY_APPLICANT,
            "query_params": {"target_id": {"param": PARAM_TARGET}},
            "read_recipe": (
                "MariaDB service 'db', SELECT from `tabLoan Application` as the "
                "read-only oracle user (registry read_recipe)"
            ),
            "connection": "read_only_oracle_user",
        },
        isolated_from_agent=True,
        trial_unique_payload=True,
        refusal_controls=refusal,
        adversarially_audited=False,  # gated on a container red-team pass
    )


def _loan_record_effect(*, count_new_only: bool = True) -> Effect:
    return record_written_effect(
        {"applicant": param(PARAM_TARGET)},
        expected_count=1,
        count_new_only=count_new_only,
        forbid_collateral_loss=True,
        risk="irreversible",
        probe="exactly one new Loan Application for this trial applicant",
        timeout_s=5.0,
    )


def _spec(
    task_id: str,
    *,
    title: str,
    category: DC,
    goal: str,
    expected_effect: Effect,
    oracle,
    initial_state: dict,
    reversible: bool = True,
    effect_declared: bool = True,
    split: str = "dev",
    notes: str = "",
) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        title=title,
        substrate=Substrate.WEB,
        category=category,
        goal=goal,
        expected_effect=expected_effect,
        oracle=oracle,
        initial_state=initial_state,
        reversible=reversible,
        effect_declared=effect_declared,
        split=split,
        notes=notes,
    )


FRAPPE_TASKS: tuple[ContainerTask, ...] = (
    ContainerTask(
        _spec(
            "frappe_c1_partial_loan_amount",
            title="Loan Application saved without the amount (partial save)",
            category=DC.C1_PARTIAL_SAVE,
            goal="Create a Loan Application for the applicant with the requested "
            "loan amount and save it.",
            expected_effect=field_equals_effect(
                {"applicant": param(PARAM_TARGET)},
                field="loan_amount",
                value=param(PARAM_NOTE),
                probe="the saved Loan Application carries the requested amount",
                timeout_s=5.0,
            ),
            oracle=_sql_oracle(
                "SQL read-back of loan_amount for the trial applicant",
            ),
            initial_state={"reset": "baseline_sql_dump"},
            notes="The row persists but loan_amount is dropped (0/NULL); the "
            "form shows saved.",
        ),
        correct_action_available=False,
        needs_container=ENVIRONMENT,
    ),
    ContainerTask(
        _spec(
            "frappe_c2_duplicate_application",
            title="Double-submitted Loan Application (two rows)",
            category=DC.C2_DUPLICATE_SUBMISSION,
            goal="Submit one Loan Application for the applicant and save it.",
            expected_effect=_loan_record_effect(),
            oracle=_sql_oracle(
                "SQL at-most-once count of Loan Applications for the trial applicant",
            ),
            initial_state={"reset": "baseline_sql_dump"},
            reversible=False,
            notes="A non-idempotent double-submit writes two applications.",
        ),
        correct_action_available=False,
        needs_container=ENVIRONMENT,
    ),
    ContainerTask(
        _spec(
            "frappe_c3_optimistic_reject",
            title="Application 'submitted' in the UI; server rejected it",
            category=DC.C3_OPTIMISTIC_THEN_REJECT,
            goal="Submit a Loan Application for the applicant and save it.",
            expected_effect=_loan_record_effect(),
            oracle=_sql_oracle(
                "SQL presence of a Loan Application for the trial applicant",
            ),
            initial_state={"reset": "baseline_sql_dump"},
            reversible=False,
            notes="Optimistic submit banner; a validation hook rejected it — no row.",
        ),
        correct_action_available=False,
        needs_container=ENVIRONMENT,
    ),
    ContainerTask(
        _spec(
            "frappe_c4_stale_status",
            title="Concurrent status change clobbered (lost update)",
            category=DC.C4_STALE_OVERWRITE,
            goal="Set the applicant's Loan Application status to 'Approved' and save.",
            expected_effect=record_written_effect(
                {"applicant": param(PARAM_TARGET)},
                expected_count=1,
                count_new_only=False,
                forbid_collateral_loss=True,
                probe="our status edit lands without clobbering the concurrent "
                "rate change",
                timeout_s=5.0,
            ),
            oracle=_sql_oracle(
                "SQL row-state + collateral check vs the concurrently changed "
                "rate for the trial applicant",
            ),
            initial_state={
                "reset": "baseline_sql_dump",
                "seed_concurrent": "an underwriter changed the interest rate "
                "between load and save",
            },
            reversible=False,
            split="test",
            notes="Last-write-wins silently drops the concurrent rate change.",
        ),
        correct_action_available=False,
        needs_container=ENVIRONMENT,
    ),
    ContainerTask(
        _spec(
            "frappe_c5_double_delivered_submit",
            title="Double-delivered submit click (two applications)",
            category=DC.C5_DOUBLE_DELIVERED_INPUT,
            goal="Submit one Loan Application for the applicant and save it.",
            expected_effect=_loan_record_effect(),
            oracle=_sql_oracle(
                "SQL at-most-once count of Loan Applications for the trial applicant",
            ),
            initial_state={"reset": "baseline_sql_dump"},
            reversible=False,
            notes="A double-delivered click submits the application twice.",
        ),
        correct_action_available=False,
        needs_container=ENVIRONMENT,
    ),
    ContainerTask(
        _spec(
            "frappe_c6_homonym_applicant",
            title="Application filed against a same-name applicant",
            category=DC.C6_WRONG_RECORD_HOMONYM,
            goal="Two customers share a name. Create the Loan Application for "
            "the applicant identified by customer ID and save.",
            expected_effect=_loan_record_effect(),
            oracle=_sql_oracle(
                "SQL presence on the INTENDED applicant id (the same-name decoy "
                "confirmation looks identical)",
                refusal=True,
            ),
            initial_state={
                "reset": "baseline_sql_dump",
                "seed_decoy": "a same-name customer with a different customer ID",
            },
            reversible=False,
            split="test",
            notes="The intended applicant has no application; the homonym gets it.",
        ),
        correct_action_available=True,
        needs_container=ENVIRONMENT,
    ),
    ContainerTask(
        _spec(
            "frappe_c7_wrong_loan_product",
            title="Application booked under the wrong loan product",
            category=DC.C7_SILENT_NOOP_WRONG_TARGET,
            goal="Create the Loan Application under the 'Personal Loan' product "
            "for the applicant and save.",
            expected_effect=field_equals_effect(
                {"applicant": param(PARAM_TARGET)},
                field="loan_amount",
                value=param(PARAM_NOTE),
                probe="the amount is booked on the intended product, not a decoy",
                timeout_s=5.0,
            ),
            oracle=_sql_oracle(
                "SQL read-back on the intended applicant/product",
                refusal=True,
            ),
            initial_state={
                "reset": "baseline_sql_dump",
                "seed_decoy": "a look-alike loan product in the dropdown",
            },
            notes="Right action, wrong product target.",
        ),
        correct_action_available=True,
        needs_container=ENVIRONMENT,
    ),
    ContainerTask(
        _spec(
            "frappe_c1_partial_undeclared",
            title="Partial save, raw agent (no verifier configured)",
            category=DC.C1_PARTIAL_SAVE,
            goal="Create a Loan Application for the applicant with the requested "
            "amount and save it.",
            expected_effect=field_equals_effect(
                {"applicant": param(PARAM_TARGET)},
                field="loan_amount",
                value=param(PARAM_NOTE),
                probe="the saved Loan Application carries the requested amount",
                timeout_s=5.0,
            ),
            oracle=_sql_oracle(
                "SQL read-back of loan_amount for the trial applicant",
            ),
            initial_state={"reset": "baseline_sql_dump"},
            effect_declared=False,
            notes="effect_declared=False: raw-agent partial-save condition.",
        ),
        correct_action_available=False,
        needs_container=ENVIRONMENT,
    ),
    ContainerTask(
        _spec(
            "frappe_ctl_clean_application",
            title="Control: a clean, correct Loan Application",
            category=DC.CONTROL,
            goal="Create one Loan Application for the applicant with the "
            "requested amount and save.",
            expected_effect=_loan_record_effect(),
            oracle=_sql_oracle(
                "SQL at-most-once count of Loan Applications for the trial applicant",
            ),
            initial_state={"reset": "baseline_sql_dump"},
            notes="No fault: the effect-verified arm must score SUCCESS.",
        ),
        correct_action_available=True,
        needs_container=ENVIRONMENT,
    ),
    ContainerTask(
        _spec(
            "frappe_ctl_idempotent_resubmit",
            title="Control: idempotency key de-dups a retried submit",
            category=DC.CONTROL,
            goal="Submit one Loan Application for the applicant; if the page "
            "reloads, do not create a second one. Save.",
            expected_effect=record_written_effect(
                {"applicant": param(PARAM_TARGET)},
                expected_count=1,
                count_new_only=True,
                forbid_collateral_loss=True,
                idempotency_key=param(PARAM_TARGET),
                key_field="applicant",
                probe="exactly one application despite a retried submit",
                timeout_s=5.0,
            ),
            oracle=_sql_oracle(
                "SQL de-dup count on the trial applicant reference",
            ),
            initial_state={"reset": "baseline_sql_dump"},
            notes="The recommended fix: a per-applicant guard collapses the retry.",
        ),
        correct_action_available=True,
        needs_container=ENVIRONMENT,
    ),
)
