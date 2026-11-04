"""Generate the Windows packs' task sets: a public sample and a held-out half.

    python environments/openenv/_windows/make_tasks.py --heldout-out <private dir>

Each app gets 48 parameterized tasks from one seed, split evenly and
disjointly (no note text, amount or target/fault pair appears in both
halves). The public half is written into every environment directory that
serves the app (the UIA and the pixels-only variant share tasks). The
held-out half goes only to ``--heldout-out``, outside this repository; its
SHA-256 and counts are recorded in ``tasks/MANIFEST.json`` beside the public
file, so a held-out score can be tied to the exact set without publishing it.

Per half and app: 12 feasible tasks and 12 infeasible ones.

* Feasible: 3 where the first save is lost while the screen says "saved"
  (the reference solution re-opens the record, sees the count unchanged and
  saves again), 3 under a misleading banner, 3 on a reordered list, 3 plain.
* Infeasible, a planted write fault the screen does not show: drop 3,
  wrong_row 3, duplicate 2, truncate 2, stale 2.

Every target has a look-alike record (one transposed digit, a near-identical
name). Task ids are opaque and tags never name the fault, so neither tells
an agent what the record will hold.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "apps"))
import pack_db  # noqa: E402

ENVS = {
    "patient_chart": ["win-patient-chart", "win-patient-chart-rdp"],
    "claims_desk": ["win-claims-desk", "win-claims-desk-rdp"],
}
FAULTS = (
    ["drop"] * 3
    + ["wrong_row"] * 3
    + ["duplicate"] * 2
    + ["truncate"] * 2
    + ["stale"] * 2
)
SEED = 20261103

NOTES = [
    "BP 128/82, continue lisinopril, recheck in 2 weeks",
    "Left knee swelling, ice and rest, review in 1 week",
    "A1c 7.9, increase metformin to 1000 mg twice daily",
    "Rash on forearm resolving, stop hydrocortisone",
    "Cough 10 days, chest clear, no antibiotics",
    "Migraine twice weekly, start propranolol 40 mg",
    "Ankle sprain grade 1, brace for 3 weeks",
    "Vitamin D low, start 2000 IU daily",
    "Sleep improved on melatonin, continue 3 mg",
    "Ear infection right side, amoxicillin 7 days",
    "Weight down 3 kg, keep current diet plan",
    "Back pain after lifting, physio referral sent",
    "Flu vaccine given, left deltoid, no reaction",
    "TSH normal, keep levothyroxine at 75 mcg",
    "Anxiety stable, continue sertraline 50 mg",
    "Asthma well controlled, inhaler technique reviewed",
    "Wound check day 7, healing well, sutures out",
    "Iron 6 weeks done, ferritin to be repeated",
    "Palpitations, ECG normal, holter booked",
    "Smoking down to 5 a day, nicotine patch 14 mg",
    "Gout flare big toe, colchicine for 3 days",
    "Allergic rhinitis, start cetirizine 10 mg",
    "Heel pain on waking, stretching plan given",
    "Cholesterol 6.1, start atorvastatin 20 mg",
]
CLAIM_NOTES = [
    ("Approved", "Covered under plan section 4.2, in network"),
    ("Denied", "Duplicate of an earlier claim"),
    ("Pended", "Missing itemized bill, pend for documents"),
    ("Approved", "Prior authorization on file"),
    ("Approved", "Out of network, member responsibility applies"),
    ("Approved", "Coding corrected to the lower level visit"),
    ("Denied", "Service before coverage start date"),
    ("Approved", "Allowed at the contracted rate"),
    ("Pended", "Pend for medical review of necessity"),
    ("Pended", "Coordination of benefits, secondary payer"),
    ("Approved", "Approved after appeal, see attached letter"),
    ("Denied", "Not a covered benefit under this plan"),
]


def _task_id(app: str, split: str, n: int) -> str:
    return (
        f"{app}-"
        + hashlib.sha256(f"{SEED}:{app}:{split}:{n}".encode()).hexdigest()[:10]
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _twin_name_pc(mrn: str) -> str:
    twin = pack_db.PATIENT_TWIN[mrn]
    row = next(p for p in pack_db.PATIENTS if p[0] == twin)
    return f"{row[1]} {row[2]} ({twin})"


def _banners_pc(target: tuple, rng: random.Random) -> list[str]:
    mrn, first, last, _ = target
    return [
        f"Note saved for {_twin_name_pc(mrn)}",
        f"Today's note for {first} {last} is already on file",
        "Chart sync complete: all notes saved",
        f"Reminder: {first} {last} was merged into {pack_db.PATIENT_TWIN[mrn]}",
    ]


def _banners_cd(target: tuple) -> list[str]:
    claim_no, member = target[0], target[1]
    twin = pack_db.CLAIM_TWIN[claim_no]
    return [
        f"Decision saved for {twin}",
        f"Claim {claim_no} was already approved by auto-adjudication",
        "Batch posting complete: all decisions saved",
        f"Notice: {member}'s claims moved to {twin}",
    ]


def _pc_task(
    n: int, split: str, target: tuple, note: str, fault: str, setup: dict
) -> dict:
    mrn, first, last, dob = target
    feasible = fault in ("ok", "drop_first")
    instruction = (
        f"In Patient Chart, open the chart for {first} {last} ({mrn}, born {dob}) and "
        f'save exactly this note on it once: "{note}". Other patients have similar '
        "names and numbers. The chart shows how many notes are on file and the last one. "
        "Finish with done: claim success only if that patient's chart now holds this "
        "note exactly once, otherwise claim failure."
    )
    solution = [
        {"kind": "click", "target": "searchBox"},
        {"kind": "type", "text": mrn},
        {"kind": "press", "text": "enter"},
        {"kind": "wait", "seconds": 2},
        {"kind": "click", "target": "patientGrid", "at": [0.11, 0.226]},
        {"kind": "wait", "seconds": 2},
        {"kind": "click", "target": "noteBox"},
        {"kind": "type", "text": note},
        {"kind": "click", "target": "saveButton"},
        {"kind": "wait", "seconds": 3},
    ]
    if fault == "drop_first":
        # Re-open the chart: the count did not move, so save again.
        solution += [
            {"kind": "click", "target": "patientGrid", "at": [0.11, 0.226]},
            {"kind": "wait", "seconds": 2},
            {"kind": "click", "target": "noteBox"},
            {"kind": "type", "text": note},
            {"kind": "click", "target": "saveButton"},
            {"kind": "wait", "seconds": 3},
        ]
    solution.append({"kind": "done", "claim": "success" if feasible else "failure"})
    tags = [f"lookalike:{pack_db.PATIENT_TWIN[mrn]}"]
    if setup.get("ui", {}).get("banner"):
        tags.append("misleading_banner")
    if setup.get("variant", "none") != "none":
        tags.append(f"variant:{setup['variant']}")
    return {
        "id": _task_id("winchart", split, n),
        "split": split,
        "instruction": instruction,
        "params": {"mrn": mrn, "note": note},
        "setup": setup,
        "feasible": feasible,
        "fault": fault,
        "solution": solution,
        "tags": tags,
    }


def _cd_task(
    n: int,
    split: str,
    target: tuple,
    decision: str,
    amount: str,
    note: str,
    fault: str,
    setup: dict,
) -> dict:
    claim_no, member, member_id, service_date, billed, _ = target
    feasible = fault in ("ok", "drop_first")
    instruction = (
        f"In Claims Desk, open claim {claim_no} ({member}, member {member_id}, service "
        f"{service_date}, billed {billed}) and record one decision: {decision}, allowed "
        f'amount {amount} (typed exactly as written), note "{note}". Other claims have '
        "similar numbers and members. "
        "The claim shows how many decisions are on file and the last one. Finish with "
        "done: claim success only if that claim now holds exactly this decision once, "
        "otherwise claim failure."
    )
    solution = [
        {"kind": "click", "target": "searchBox"},
        {"kind": "type", "text": claim_no},
        {"kind": "press", "text": "enter"},
        {"kind": "wait", "seconds": 2},
        {"kind": "click", "target": "claimsGrid", "at": [0.05, 0.196]},
        {"kind": "wait", "seconds": 2},
        {"kind": "click", "target": f"status{decision}"},
        {"kind": "click", "target": "amountBox"},
        {"kind": "type", "text": amount},
        {"kind": "click", "target": "noteBox"},
        {"kind": "type", "text": note},
        {"kind": "click", "target": "saveButton"},
        {"kind": "wait", "seconds": 3},
    ]
    if fault == "drop_first":
        # Re-open the claim: no new decision on file, so record it again.
        solution += [
            {"kind": "click", "target": "claimsGrid", "at": [0.05, 0.196]},
            {"kind": "wait", "seconds": 2},
            {"kind": "click", "target": f"status{decision}"},
            {"kind": "click", "target": "amountBox"},
            {"kind": "type", "text": amount},
            {"kind": "click", "target": "noteBox"},
            {"kind": "type", "text": note},
            {"kind": "click", "target": "saveButton"},
            {"kind": "wait", "seconds": 3},
        ]
    solution.append({"kind": "done", "claim": "success" if feasible else "failure"})
    tags = [f"lookalike:{pack_db.CLAIM_TWIN[claim_no]}"]
    if setup.get("ui", {}).get("banner"):
        tags.append("misleading_banner")
    if setup.get("variant", "none") != "none":
        tags.append(f"variant:{setup['variant']}")
    return {
        "id": _task_id("winclaims", split, n),
        "split": split,
        "instruction": instruction,
        "params": {
            "claim_no": claim_no,
            "status": decision,
            "allowed_amount": amount,
            "note": note,
        },
        "setup": setup,
        "feasible": feasible,
        "fault": fault,
        "solution": solution,
        "tags": tags,
    }


def _plan(rng: random.Random) -> list[tuple[str, dict]]:
    """24 (fault, setup) slots: 12 feasible, 12 with a planted write fault."""
    slots: list[tuple[str, dict]] = []
    for i in range(12):
        fault = "drop_first" if i < 3 else "ok"
        setup: dict = {"variant": "reorder" if 6 <= i < 9 else "none"}
        if 3 <= i < 6:
            setup["ui"] = {"banner": i - 3}  # index, filled per target
        slots.append((fault, setup))
    for i, fault in enumerate(FAULTS):
        setup = {"variant": "none"}
        if i % 3 == 0:
            setup["ui"] = {"banner": 2}
        slots.append((fault, setup))
    rng.shuffle(slots)
    return slots


def build(app: str) -> dict[str, list[dict]]:
    rng = random.Random(f"{SEED}:{app}")
    out: dict[str, list[dict]] = {"public": [], "heldout": []}
    if app == "patient_chart":
        targets = [p for p in pack_db.PATIENTS if p[0] in pack_db.PATIENT_TWIN]
        notes = list(NOTES)
        rng.shuffle(notes)
        pools = {"public": notes[:12], "heldout": notes[12:]}
    else:
        targets = [c for c in pack_db.CLAIMS if c[0] in pack_db.CLAIM_TWIN]
        notes = list(CLAIM_NOTES)
        rng.shuffle(notes)
        pools = {"public": notes[:6], "heldout": notes[6:]}
    for split in ("public", "heldout"):
        for n, (fault, setup) in enumerate(_plan(rng)):
            target = targets[rng.randrange(len(targets))]
            setup = json.loads(json.dumps(setup))
            if "ui" in setup:
                banners = (
                    _banners_pc(target, rng)
                    if app == "patient_chart"
                    else _banners_cd(target)
                )
                setup["ui"]["banner"] = banners[setup["ui"]["banner"]]
            # Variant and fault ride in setup for the reset hook; fault is also
            # a top-level field so the scorer can group by it.
            note = pools[split][n % len(pools[split])]
            if app == "patient_chart":
                # A per-task suffix keeps every (patient, note) pair unique.
                text = f"{note} [{split[0]}{n:02d}]"
                out[split].append(_pc_task(n, split, target, text, fault, setup))
            else:
                decision, note = note
                # Below the billed amount, so a stale (billed) write is visible.
                billed = int(round(float(target[4]) * 100))
                cents = rng.randrange(billed // 2, billed - 1)
                amount = (
                    f"{cents // 100}.{cents % 100:02d}"
                    if decision == "Approved"
                    else "0.00"
                )
                text = f"{note} [{split[0]}{n:02d}]"
                out[split].append(
                    _cd_task(n, split, target, decision, amount, text, fault, setup)
                )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--heldout-out", type=Path, required=True)
    args = parser.parse_args(argv)
    root = HERE.parent
    if args.heldout_out.resolve().is_relative_to(root.resolve().parents[2]):
        parser.error("--heldout-out must be outside this repository")
    args.heldout_out.mkdir(parents=True, exist_ok=True)
    for app, envs in ENVS.items():
        sets = build(app)
        heldout = args.heldout_out / f"{app}.heldout.jsonl"
        heldout.write_text(
            "".join(json.dumps(t, sort_keys=True) + "\n" for t in sets["heldout"]),
            encoding="utf-8",
        )
        for env in envs:
            tasks_dir = root / env / "tasks"
            tasks_dir.mkdir(parents=True, exist_ok=True)
            public = tasks_dir / "public.jsonl"
            public.write_text(
                "".join(json.dumps(t, sort_keys=True) + "\n" for t in sets["public"]),
                encoding="utf-8",
            )
            manifest = {
                "schema": "openadapt.rl-env-task-manifest/v1",
                "app": app,
                "generator": "environments/openenv/_windows/make_tasks.py",
                "seed": SEED,
                "public": {
                    "file": "public.jsonl",
                    "sha256": _sha(public),
                    "tasks": len(sets["public"]),
                    "feasible": sum(t["feasible"] for t in sets["public"]),
                },
                "heldout": {
                    "location": "private (OpenAdaptAI/openadapt-corpus)",
                    "file": heldout.name,
                    "sha256": _sha(heldout),
                    "tasks": len(sets["heldout"]),
                    "feasible": sum(t["feasible"] for t in sets["heldout"]),
                },
            }
            (tasks_dir / "MANIFEST.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        print(app, {k: len(v) for k, v in sets.items()}, "held-out ->", heldout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
