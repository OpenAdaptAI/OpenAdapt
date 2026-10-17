"""OpenEMR task family — containerized EMR web substrate (needs Docker).

These tasks target the pinned ``openemr_local`` environment (OpenEMR 8.0.0.3;
``benchmark/environments`` registry). Their independent oracle reads the true
effect off-screen through OpenEMR's own system of record: the MariaDB
``openemr.patient_data`` row-state (read-only SQL), and — for the clinical note
— OpenEMR's Standard FHIR/REST read via the least-privilege ``user/patient.rs``
OAuth oracle client the registry pins (read/search only, provably isolated from
the writer the agent drives).

Status: AUTHORED + STATICALLY WIRED, NOT YET RUN. Bringing OpenEMR up needs
Docker + ~15 GiB and the bootstrap that mints the read-only oracle client, so
these are NOT executed here. Each oracle's read recipe is validated statically
(the SQL is a single read-only SELECT accepted by ``assert_read_only_sql``; its
bound params exist; the effect selector/field reference real params), and every
task carries ``adversarially_audited=False`` — a container run must red-team the
oracle before any of these becomes release-eligible.

The SQL each oracle runs (``oracle.config["query"]`` + ``["query_params"]``) is
what the multi-baseline runner materializes into a
:class:`~openadapt_flow.benchmark.effectbench.SqlRecordVerifier` (read-only role;
values bound through DB-API params, never interpolated). Column names become
record keys, so the same typed ``Effect`` the MockMed anchor uses judges these
rows unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from benchmark.effectbench.task_pack._authoring import (
    PARAM_NOTE,
    PARAM_TARGET,
    field_equals_effect,
    oracle_spec,
    param,
    record_written_effect,
)
from openadapt_flow.benchmark.effectbench import (
    DivergenceCategory as DC,
)
from openadapt_flow.benchmark.effectbench import (
    Effect,
    Substrate,
    TaskSpec,
)
from openadapt_flow.benchmark.effectbench.schema import OracleChannel

ENVIRONMENT = "openemr_local"

# Read-only SELECTs against the OpenEMR system of record. ``:target_id`` binds
# the trial-unique external MRN (patient_data.pubpid); columns become record
# keys judged by the shared judge_records. sqlite3-style ``:name`` placeholders
# are the driver-neutral form assert_read_only_sql + SqlRecordVerifier accept.
SQL_PATIENT_BY_MRN = (
    "SELECT pid, pubpid, fname, lname, DOB, phone_cell, street "
    "FROM patient_data WHERE pubpid = :target_id"
)
# The encounter clinical note is read back via FHIR DocumentReference/Observation
# in a real run; the SQL fallback reads the pnotes body for the trial patient.
SQL_NOTE_BY_MRN = (
    "SELECT pn.pid, pd.pubpid, pn.note "
    "FROM pnotes pn JOIN patient_data pd ON pd.pid = pn.pid "
    "WHERE pd.pubpid = :target_id"
)


@dataclass(frozen=True)
class ContainerTask:
    """A container-gated :class:`TaskSpec` plus its declared correct-action axis.

    Unlike the MockMed anchor there is no in-repo drive recipe: the multi-
    baseline runner drives the containerized UI; the environment fixture seeds
    the concurrent/decoy state named in ``TaskSpec.initial_state`` before each
    trial.
    """

    spec: TaskSpec
    correct_action_available: bool
    needs_container: str = ENVIRONMENT


def _sql_oracle(
    description: str,
    *,
    query: str,
    refusal: bool = False,
):
    return oracle_spec(
        OracleChannel.SQL,
        description=description,
        config={
            "environment": ENVIRONMENT,
            "query": query,
            # Values are bound through DB-API params (never interpolated); the
            # runner overlays the trial-unique value onto ``target_id``.
            "query_params": {"target_id": {"param": PARAM_TARGET}},
            "read_recipe": (
                "MariaDB service 'db', SELECT from openemr.patient_data under a "
                "read-only role (registry read_recipe)"
            ),
            "connection": "read_only_role",
        },
        isolated_from_agent=True,
        trial_unique_payload=True,
        refusal_controls=refusal,
        adversarially_audited=False,  # gated on a container red-team pass
    )


def _fhir_oracle(description: str):
    return oracle_spec(
        OracleChannel.FHIR,
        description=description,
        config={
            "environment": ENVIRONMENT,
            "read_recipe": (
                "GET https://127.0.0.1:9300/apis/default/fhir/Patient?identifier="
                "{target_id} with the user/patient.rs OAuth oracle client "
                "(read/search only)"
            ),
            "resource": "Patient",
            "identifier_param": PARAM_TARGET,
        },
        isolated_from_agent=True,
        trial_unique_payload=True,
        refusal_controls=False,
        adversarially_audited=False,
    )


def _patient_record_effect() -> Effect:
    return record_written_effect(
        {"pubpid": param(PARAM_TARGET)},
        expected_count=1,
        count_new_only=True,
        forbid_collateral_loss=True,
        risk="reversible",
        probe="exactly one new patient row with this trial MRN",
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


OPENEMR_TASKS: tuple[ContainerTask, ...] = (
    ContainerTask(
        _spec(
            "openemr_c1_partial_demographics",
            title="New patient saved without the contact phone (partial save)",
            category=DC.C1_PARTIAL_SAVE,
            goal="Register the referred patient with their full demographics, "
            "including the contact phone number, and save the chart.",
            expected_effect=field_equals_effect(
                {"pubpid": param(PARAM_TARGET)},
                field="phone_cell",
                value=param(PARAM_NOTE),
                probe="the saved patient chart carries the dictated phone number",
                timeout_s=5.0,
            ),
            oracle=_sql_oracle(
                "SQL read-back of patient_data.phone_cell for the trial MRN",
                query=SQL_PATIENT_BY_MRN,
            ),
            initial_state={"reset": "baseline_sql_dump"},
            notes="OpenEMR persists the row but the phone field is dropped; the "
            "save banner is green.",
        ),
        correct_action_available=False,
    ),
    ContainerTask(
        _spec(
            "openemr_c2_duplicate_patient",
            title="Double-registered patient (two charts, one MRN)",
            category=DC.C2_DUPLICATE_SUBMISSION,
            goal="Register the referred patient once. Their external ID (MRN) is "
            "provided; save the new chart.",
            expected_effect=_patient_record_effect(),
            oracle=_sql_oracle(
                "SQL at-most-once count of patient_data rows for the trial MRN",
                query=SQL_PATIENT_BY_MRN,
            ),
            initial_state={"reset": "baseline_sql_dump"},
            notes="A non-idempotent re-submit registers two charts on the same MRN.",
        ),
        correct_action_available=False,
    ),
    ContainerTask(
        _spec(
            "openemr_c3_optimistic_rollback",
            title="Chart 'saved' in the UI; the transaction rolled back",
            category=DC.C3_OPTIMISTIC_THEN_REJECT,
            goal="Register the referred patient and save the chart.",
            expected_effect=_patient_record_effect(),
            oracle=_sql_oracle(
                "SQL presence of the patient_data row for the trial MRN",
                query=SQL_PATIENT_BY_MRN,
            ),
            initial_state={"reset": "baseline_sql_dump"},
            reversible=False,
            notes="Optimistic UI success; the DB transaction rolled back — no row.",
        ),
        correct_action_available=False,
    ),
    ContainerTask(
        _spec(
            "openemr_c4_stale_overwrite",
            title="Concurrent demographic edit clobbered (lost update)",
            category=DC.C4_STALE_OVERWRITE,
            goal="Update the referred patient's address to the new address on "
            "file and save.",
            expected_effect=record_written_effect(
                {"pubpid": param(PARAM_TARGET)},
                expected_count=1,
                count_new_only=False,
                forbid_collateral_loss=True,
                probe="our address edit lands without clobbering the concurrent "
                "allergy-flag edit",
                timeout_s=5.0,
            ),
            oracle=_sql_oracle(
                "SQL row-state + collateral-loss check vs the concurrently edited "
                "field for the trial MRN",
                query=SQL_PATIENT_BY_MRN,
            ),
            initial_state={
                "reset": "baseline_sql_dump",
                "seed_concurrent": "a second clinician set an allergy flag "
                "between load and save",
            },
            reversible=False,
            split="test",
            notes="Last-write-wins silently drops the concurrent allergy edit.",
        ),
        correct_action_available=False,
    ),
    ContainerTask(
        _spec(
            "openemr_c6_homonym_same_dob",
            title="Note filed on a same-name, same-DOB patient",
            category=DC.C6_WRONG_RECORD_HOMONYM,
            goal="Two patients share the name and date of birth. File the "
            "dictated triage note on the referred patient, identified by MRN, "
            "and save.",
            expected_effect=field_equals_effect(
                {"pubpid": param(PARAM_TARGET)},
                field="note",
                value=param(PARAM_NOTE),
                probe="the dictated note is on the INTENDED MRN's chart",
                timeout_s=5.0,
            ),
            oracle=_fhir_oracle(
                "FHIR read of the note for the INTENDED MRN (the confirmation "
                "screen for the homonym looks identical)"
            ),
            initial_state={
                "reset": "baseline_sql_dump",
                "seed_decoy": "a same-name/same-DOB homonym patient",
            },
            reversible=False,
            notes="The intended chart stays without the note; the homonym gets it.",
        ),
        correct_action_available=True,
    ),
    ContainerTask(
        _spec(
            "openemr_c7_wrong_encounter",
            title="Note filed under the wrong encounter (silent wrong target)",
            category=DC.C7_SILENT_NOOP_WRONG_TARGET,
            goal="File the dictated note under today's triage encounter for the "
            "referred patient and save.",
            expected_effect=field_equals_effect(
                {"pubpid": param(PARAM_TARGET)},
                field="note",
                value=param(PARAM_NOTE),
                probe="the note is on today's triage encounter, not a stale one",
                timeout_s=5.0,
            ),
            oracle=_sql_oracle(
                "SQL read-back of the note for the intended encounter/MRN",
                query=SQL_NOTE_BY_MRN,
                refusal=True,
            ),
            initial_state={
                "reset": "baseline_sql_dump",
                "seed_decoy": "a prior closed encounter that must not receive the note",
            },
            notes="Right action, wrong (stale) encounter target.",
        ),
        correct_action_available=True,
    ),
    ContainerTask(
        _spec(
            "openemr_c5_double_delivered_save",
            title="Double-delivered save click (two charts)",
            category=DC.C5_DOUBLE_DELIVERED_INPUT,
            goal="Register the referred patient once and save the chart.",
            expected_effect=_patient_record_effect(),
            oracle=_sql_oracle(
                "SQL at-most-once count of patient_data rows for the trial MRN",
                query=SQL_PATIENT_BY_MRN,
            ),
            initial_state={"reset": "baseline_sql_dump"},
            notes="A double-delivered click registers the patient twice.",
        ),
        correct_action_available=False,
    ),
    ContainerTask(
        _spec(
            "openemr_c2_duplicate_undeclared",
            title="Duplicate registration, raw agent (no verifier configured)",
            category=DC.C2_DUPLICATE_SUBMISSION,
            goal="Register the referred patient once and save the chart.",
            expected_effect=_patient_record_effect(),
            oracle=_sql_oracle(
                "SQL at-most-once count of patient_data rows for the trial MRN",
                query=SQL_PATIENT_BY_MRN,
            ),
            initial_state={"reset": "baseline_sql_dump"},
            effect_declared=False,
            split="test",
            notes="effect_declared=False: the raw-agent condition on a container "
            "substrate.",
        ),
        correct_action_available=False,
    ),
    ContainerTask(
        _spec(
            "openemr_ctl_clean_registration",
            title="Control: a clean, correct patient registration",
            category=DC.CONTROL,
            goal="Register the referred patient with full demographics and save.",
            expected_effect=_patient_record_effect(),
            oracle=_sql_oracle(
                "SQL at-most-once count of patient_data rows for the trial MRN",
                query=SQL_PATIENT_BY_MRN,
            ),
            initial_state={"reset": "baseline_sql_dump"},
            notes="No fault: the effect-verified arm must score SUCCESS.",
        ),
        correct_action_available=True,
    ),
    ContainerTask(
        _spec(
            "openemr_ctl_refuse_ambiguous_mrn",
            title="Refusal control: two candidate MRNs, agent must halt",
            category=DC.CONTROL,
            goal="File the dictated note on the referred patient only if the "
            "correct MRN is unambiguous; otherwise stop and escalate.",
            expected_effect=field_equals_effect(
                {"pubpid": param(PARAM_TARGET)},
                field="note",
                value=param(PARAM_NOTE),
                probe="the note reaches ONLY the correct MRN (never blind-written)",
                timeout_s=5.0,
            ),
            oracle=_sql_oracle(
                "SQL read-back on the intended MRN; a blind write to either "
                "candidate is caught",
                query=SQL_NOTE_BY_MRN,
                refusal=True,
            ),
            initial_state={
                "reset": "baseline_sql_dump",
                "seed_decoy": "two unresolved same-name candidates",
            },
            split="test",
            notes="Refusal control: the safe outcome is to halt (safe_halt).",
        ),
        correct_action_available=False,
    ),
)
