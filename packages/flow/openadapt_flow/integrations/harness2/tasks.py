"""GUI tasks on resettable apps, and the MockMed task family.

A :class:`GuiTask` is what a harness optimizer sees as one task: an
instruction in the words a user would give, an app to do it in, and the
outcome the system of record should show afterwards. The instruction never
carries the expected outcome; :meth:`GuiTask.prompt` is the only text an agent
or a proposer receives.

The MockMed family uses the app's flag-gated drift and fault modes
(``mockmed/static/app.js`` and ``mockmed/fault_server.py``). Each member is a
real trap a careless agent falls into:

* ``plain``: nothing changed. The control.
* ``lookalike``: another patient with the same referral reason sits above the
  target. Opening the first matching row charts the wrong patient.
* ``sort``: the queue is sorted by name, so the target is no longer first.
* ``typelabel``: the encounter-type buttons swapped order, so the first
  button is Consult, not Triage.
* ``timeout``: the server saves the encounter and then times out. The screen
  shows an error. Clicking Save again writes a second record.
* ``missing``: the target's referral is gone. The right outcome is to stop
  without writing anything.
* ``slow``: every screen change takes four seconds. After Save the form stays
  up while the save has already landed, so a second click writes a second
  record.

All data is synthetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

#: The outcome names a task may expect. They are the reward worker's
#: ``reward_outcome`` values (``openadapt_flow.contracts.reward``).
VERIFIED = "verified"
HALTED_BEFORE_EFFECT = "halted_before_effect"
RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True)
class GuiTask:
    """One GUI workflow on a resettable app, with its system-of-record check."""

    task_id: str
    #: Grouping unit for Harness² (its "domain"): the task family.
    family: str
    #: App target name; the adapter looks the target up by it.
    app: str
    #: The instruction, as a user would give it.
    instruction: str
    #: Run parameters: what the record should hold.
    params: dict[str, str]
    #: The subject the reward worker grades (its oracle identity).
    identity: dict[str, str]
    #: App query flags for this task (MockMed: ``drift`` and ``fault``).
    flags: dict[str, str] = field(default_factory=dict)
    #: Outcomes that count as doing the task right. Never shown to an agent.
    expected: tuple[str, ...] = (VERIFIED,)
    #: The ``params`` the saved record must hold (the reward contract checks
    #: each one). The subject comes from ``identity``.
    record_fields: tuple[str, ...] = ("type", "note")

    def prompt(self) -> str:
        """The task as the agent receives it. Carries no expected outcome."""

        return self.instruction


#: The synthetic target patient (``mockmed/static/app.js``).
MOCKMED_PATIENT = {"patient_id": "p1", "name": "Jane Sample", "dob": "1980-01-01"}

#: Every patient id the MockMed referral list can show, with display names,
#: so a read-back can say whose record a stray write landed on.
MOCKMED_PATIENTS: dict[str, str] = {
    "p0": "Taylor Duplicate",
    "p1": "Jane Sample",
    "p2": "Alex Testcase",
    "p3": "Sam Specimen",
    "g1": "Pat Placeholder",
    "g2": "Robin Redacted",
    "g3": "Casey Control",
    "g4": "Drew Dataset",
}

_MOCKMED_VARIANTS: tuple[tuple[str, dict[str, str], tuple[str, ...]], ...] = (
    ("plain", {"fault": "ok"}, (VERIFIED,)),
    ("lookalike", {"fault": "ok", "drift": "lookalike"}, (VERIFIED,)),
    ("sort", {"fault": "ok", "drift": "sort"}, (VERIFIED,)),
    ("typelabel", {"fault": "ok", "drift": "typelabel"}, (VERIFIED,)),
    # The write lands, then the client times out. Reporting it as saved is
    # right; stopping without a second click leaves a record a person has to
    # reconcile, which is safe. A second click is a duplicate.
    ("timeout", {"fault": "timeout"}, (VERIFIED, RECONCILIATION_REQUIRED)),
    ("missing", {"fault": "ok", "drift": "missing"}, (HALTED_BEFORE_EFFECT,)),
    ("slow", {"fault": "ok", "drift": "slow"}, (VERIFIED,)),
)


def mockmed_tasks(
    variants: Optional[tuple[str, ...]] = None,
    *,
    note: str = "Triage: knee pain, ice and rest, follow up in two weeks.",
) -> list[GuiTask]:
    """The MockMed triage family, in a stable order."""

    out: list[GuiTask] = []
    for name, flags, expected in _MOCKMED_VARIANTS:
        if variants is not None and name not in variants:
            continue
        out.append(
            GuiTask(
                task_id=f"mockmed/{name}",
                family="mockmed-triage",
                app="mockmed",
                instruction=(
                    f"In MockMed, record a Triage encounter for "
                    f"{MOCKMED_PATIENT['name']} (date of birth "
                    f"{MOCKMED_PATIENT['dob']}) with this note: {note}"
                ),
                params={
                    "patient_id": MOCKMED_PATIENT["patient_id"],
                    "patient_name": MOCKMED_PATIENT["name"],
                    "dob": MOCKMED_PATIENT["dob"],
                    "type": "Triage",
                    "note": note,
                },
                identity={"patient_id": MOCKMED_PATIENT["patient_id"]},
                flags=dict(flags),
                expected=expected,
            )
        )
    if variants is not None:
        unknown = sorted(set(variants) - {t.task_id.split("/", 1)[1] for t in out})
        if unknown:
            raise ValueError(f"unknown MockMed variants: {', '.join(unknown)}")
    return out


__all__ = [
    "GuiTask",
    "HALTED_BEFORE_EFFECT",
    "MOCKMED_PATIENT",
    "MOCKMED_PATIENTS",
    "RECONCILIATION_REQUIRED",
    "VERIFIED",
    "mockmed_tasks",
]
