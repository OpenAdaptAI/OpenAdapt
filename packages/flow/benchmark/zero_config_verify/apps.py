"""App specs for the zero-config verifier benchmark (data only).

Everything app-specific lives here: which fault server to start, where the
hand-written ``verify.yaml`` is, which record fields the ground truth compares,
the demonstration case and the trial cases. The cases use the task builders in
``environments/openenv/make_mock_tasks.py``, so each trial drives the same
selector solution the RL environment's oracle uses.

All patients, loans, notes and memos are made up.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FLOW = Path(__file__).resolve().parents[2]
OPENENV = FLOW / "environments" / "openenv"


def _task_builders() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_zcv_make_mock_tasks", OPENENV / "make_mock_tasks.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TASKS = _task_builders()

#: (fault, record, type-or-product, free text, seed a concurrent row)
Case = tuple[str, str, str, str, bool]


@dataclass(frozen=True)
class AppSpec:
    name: str
    #: ``module:function`` returning ``(url, db, stop)``.
    serve: str
    verify_yaml: Path
    #: Record field -> task parameter, for the ground truth.
    fields: dict[str, str]
    #: Fields every correct new record carries with a fixed value.
    constants: dict[str, str]
    #: Attribute on the fault server's ``db`` naming its SQLite file, if any.
    sqlite_attr: str | None
    builder: str
    demo: Case
    trials: list[Case] = field(default_factory=list)
    held_out: bool = False

    def tasks(self, cases: list[Case]) -> list[dict[str, Any]]:
        return getattr(TASKS, self.builder)(
            cases, split="bench", prefix=f"{self.name}-"
        )


MOCKMED = AppSpec(
    name="mockmed",
    serve="openadapt_flow.mockmed.fault_server:serve",
    verify_yaml=OPENENV / "mockmed" / "verify.yaml",
    fields={"patient_id": "patient_id", "type": "type", "note": "note"},
    constants={},
    sqlite_attr=None,
    builder="mockmed_tasks",
    demo=("ok", "p1", "Triage", "Demo run: sore throat, fluids and rest", False),
    trials=[
        ("ok", "p2", "Consult", "Shoulder strain, physio twice weekly", False),
        ("ok", "p3", "Triage", "Mild fever overnight, recheck tomorrow", False),
        ("ok", "p1", "Consult", "Asthma review, inhaler technique checked", False),
        ("ok", "p2", "Triage", "Ankle sprain, compression and elevation", True),
        ("ok", "p3", "Consult", "Eczema flare, steroid cream for 5 days", True),
        ("ok", "p1", "Triage", "Headache resolved, no further action", True),
        (
            "idempotent",
            "p2",
            "Consult",
            "Blood pressure log reviewed, no change",
            False,
        ),
        ("idempotent", "p3", "Triage", "Insect bite, antihistamine advised", False),
        (
            "idempotent",
            "p1",
            "Consult",
            "Sleep diary reviewed, follow up in 4 weeks",
            True,
        ),
        ("timeout", "p3", "Consult", "Thyroid results normal, annual recheck", False),
        ("timeout", "p1", "Triage", "Wrist pain after fall, x-ray requested", False),
        ("timeout", "p2", "Triage", "Cough for 3 days, no red flags", True),
        ("partial", "p2", "Triage", "Back pain, heat pack and gentle stretches", False),
        ("partial", "p3", "Consult", "Migraine plan updated, triptan as needed", False),
        ("partial", "p1", "Consult", "Hay fever, nasal spray daily", True),
        ("optimistic", "p3", "Triage", "Nosebleed stopped with pressure", False),
        ("optimistic", "p1", "Consult", "Vitamin D low, supplement started", False),
        ("optimistic", "p2", "Consult", "Knee brace fitted, review in 6 weeks", True),
        ("session", "p1", "Triage", "Earache, drops for one week", False),
        ("session", "p2", "Consult", "Diet review, dietitian referral", False),
        ("session", "p3", "Triage", "Minor burn dressed, keep dry", True),
        ("duplicate", "p3", "Consult", "Anaemia follow-up, iron continued", False),
        ("duplicate", "p2", "Triage", "Sinus pressure, saline rinse", False),
        ("duplicate", "p1", "Triage", "Bruised rib, simple analgesia", True),
        ("double", "p2", "Consult", "Cholesterol recheck in 3 months", False),
        ("double", "p3", "Triage", "Splinter removed, wound clean", False),
        ("double", "p1", "Consult", "Smoking cessation plan agreed", True),
        ("stale", "p1", "Triage", "Repeat prescription for antihistamine", True),
        ("stale", "p1", "Consult", "Physio progress good, continue exercises", True),
        ("stale", "p1", "Triage", "Dizziness on standing, fluids advised", True),
        ("no_persist", "p3", "Triage", "Sunburn, aloe and shade", False),
        ("no_persist", "p2", "Consult", "Hearing test booked", False),
        ("no_persist", "p1", "Triage", "Mouth ulcer, salt rinse", True),
        ("wrong_record", "p1", "Consult", "Gout flare, colchicine short course", False),
        ("wrong_record", "p3", "Triage", "Stye on left eyelid, warm compress", False),
        ("wrong_record", "p1", "Triage", "Hiccups resolved without treatment", True),
    ],
)

MOCKLOAN = AppSpec(
    name="mockloan",
    serve="openadapt_flow.mockloan.fault_server:serve",
    verify_yaml=OPENENV / "mockloan" / "verify.yaml",
    fields={
        "loan_id": "loan_id",
        "product": "product",
        "amount": "amount",
        "memo": "memo",
    },
    constants={"surface": "disbursements"},
    sqlite_attr="database_path",
    builder="mockloan_tasks",
    held_out=True,
    demo=("ok", "L1001", "Personal", "Demo run: first tranche to borrower", False),
    trials=[
        ("ok", "L1002", "Auto", "Vehicle purchase, dealer invoice 4471", False),
        ("ok", "L1003", "Personal", "Home repairs, roof contractor", False),
        ("ok", "L1001", "Auto", "Second tranche, title received", False),
        ("ok", "L1002", "Personal", "Debt consolidation, card payoff", True),
        ("ok", "L1003", "Auto", "Used car, private sale", True),
        ("ok", "L1001", "Personal", "Tuition payment, spring term", True),
        ("idempotent", "L1002", "Auto", "Lease buyout, final payment", False),
        ("idempotent", "L1003", "Personal", "Medical bills, clinic invoice", False),
        ("idempotent", "L1001", "Auto", "Motorcycle purchase", True),
        ("timeout", "L1003", "Auto", "Truck down payment", False),
        ("timeout", "L1001", "Personal", "Wedding venue deposit", False),
        ("timeout", "L1002", "Personal", "Moving costs, freight quote", True),
        ("partial", "L1002", "Personal", "Furniture order 2290", False),
        ("partial", "L1003", "Auto", "Camper van purchase", False),
        ("partial", "L1001", "Auto", "Fleet vehicle, unit 7", True),
        ("optimistic", "L1003", "Personal", "Dental work, phase one", False),
        ("optimistic", "L1001", "Auto", "Electric car, charger included", False),
        ("optimistic", "L1002", "Auto", "Boat trailer purchase", True),
        ("session", "L1001", "Personal", "Bridge loan, closing costs", False),
        ("session", "L1002", "Auto", "Hybrid sedan purchase", False),
        ("session", "L1003", "Personal", "Solar panels, installer deposit", True),
        ("duplicate", "L1003", "Auto", "Delivery van, invoice 882", False),
        ("duplicate", "L1002", "Personal", "Home office equipment", False),
        ("duplicate", "L1001", "Personal", "Business licence fees", True),
        ("double", "L1002", "Auto", "Pickup truck, trade-in applied", False),
        ("double", "L1003", "Personal", "Vet bills, emergency visit", False),
        ("double", "L1001", "Auto", "Compact car, cash balance", True),
        ("stale", "L1001", "Personal", "Renovation draw two", True),
        ("stale", "L1001", "Auto", "Replacement vehicle after accident", True),
        ("stale", "L1001", "Personal", "Inventory purchase, spring stock", True),
        ("collateral", "L1002", "Personal", "Course fees, evening classes", False),
        ("collateral", "L1003", "Auto", "Classic car restoration", False),
        ("collateral", "L1001", "Personal", "Kitchen appliances", True),
    ],
)

APPS = [MOCKMED, MOCKLOAN]
