"""Reference template: referral triage from a fax folder into a desktop EMR.

PROTOTYPE, SYNTHETIC DATA ONLY. A template, not part of OpenAdapt's core.

    fax folder -> parsed document -> staff confirm exceptions
      -> one physician decision -> EMR entry (new chart, or existing match)
      -> close, reopen by record id, read back -> Seal -> L1 directory

A name-and-date-of-birth search that finds more than one chart stops the run
and puts the referral in the review queue. Nothing is written for it.

    python referral_triage.py --inbox fax_inbox --run-dir out/referrals \\
        --l1-dir out/extractions [--answers answers.json]
"""

from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path
from typing import Any

from evidence import EXECUTOR, read_field
from pipeline import People, Spec, decide, finish, intake, result_exchange
from x11 import DriverHalt, X11App, pane_value

WORKFLOW = "example.referral_triage"
EXTENSION = "org.openadapt.example.referral"
SPECS = [
    Spec("subject_name", "text", ["patient"]),
    Spec("date_of_birth", "date", ["dob"], r"\d{4}-\d{2}-\d{2}"),
    Spec("subject_id", "code", ["health card"], r"TEST-\d{4}-\d{4}"),
    Spec("referring_clinic", "text", ["referring clinic"]),
    Spec("reason_code", "code", ["reason code"], r"TEST-R\d+"),
]
PRIORITIES = ["urgent", "soon", "routine", "decline"]


def value(ex: dict[str, Any], name: str) -> str:
    return next(f["value"] for f in ex["fields"] if f["name"] == name)


def triage_one(
    emr: X11App, fax: Path, people: People, run_dir: Path, l1_dir: Path, log
) -> str:
    log(f"\n== {fax.name}")
    today = date.today().isoformat()
    req = intake(
        fax,
        SPECS,
        run_dir,
        workflow=WORKFLOW,
        artifact={
            "source_type": "fax_folder",
            "file_number": "pending",
            "date": today,
            "doctype": "referral",
        },
        people=people,
        staff_role="intake_staff",
        log=log,
    )
    name, dob, sid = (
        value(req, n) for n in ("subject_name", "date_of_birth", "subject_id")
    )
    req["artifacts"][0]["file_number"] = sid
    clinic = next(f for f in req["fields"] if f["name"] == "referring_clinic")
    req["fields"].remove(clinic)  # domain detail travels as an extension
    req["extensions"][EXTENSION] = {"referring_clinic": clinic["value"]}
    req["identities"] = [
        {
            "identity_id": "subject",
            "role": "subject",
            "fields": ["subject_name", "date_of_birth", "subject_id"],
            "match": {"status": "not_checked"},
        }
    ]
    priority = decide(
        req,
        people,
        question="What priority should this referral get?",
        options=PRIORITIES,
        role="physician",
        output_field="priority",
        log=log,
    )

    res = result_exchange(
        req,
        artifact={
            "artifact_id": "chart",
            "source_type": "rdp_screen",
            "file_number": sid,
            "date": today,
            "doctype": "triage-entry",
        },
    )
    ident = {
        "identity_id": "subject",
        "role": "subject",
        "fields": ["search_matches"],
        "match": {"status": "not_checked"},
    }
    res["identities"] = [ident]

    def halt(reason: str) -> str:
        res["outcome"], res["halt_reason"] = "halted", reason
        log(f"  HALT: {reason}")
        finish(req, res, run_dir, l1_dir, log)
        return "halted"

    if priority == "decline":
        res["outcome"] = "committed"
        res["fields"].append(
            {"name": "search_matches", "type": "integer", "value": 0, "confidence": 1.0}
        )
        ident["match"] = {"status": "new", "candidates": 0, "checked_by": EXECUTOR}
        finish(req, res, run_dir, l1_dir, log)
        return "declined"

    # Identity: search by name and date of birth.
    emr.key("Escape")
    emr.wait_screen("Search")
    emr.type(f"{name} {dob}")
    emr.key("Return")
    n = int(emr.wait_screen(r"Search results \((\d+)\)").group(1))
    listing = emr.copy_pane()
    res["fields"].append(
        {
            "name": "search_matches",
            "type": "integer",
            "value": n,
            "confidence": 1.0,
            "reads": [
                {
                    "channel": "clipboard",
                    "value": str(n),
                    "artifact_id": "chart",
                    "session": "acting",
                }
            ],
        }
    )
    log(f"  search {name} {dob}: {n} match(es)")
    if n > 1:
        ident["match"] = {
            "status": "ambiguous",
            "candidates": n,
            "checked_by": EXECUTOR,
        }
        return halt(
            f"{n} charts match this name and date of birth; nothing was written"
        )

    if n == 0:
        log("  branch: new chart")
        emr.key("ctrl+n")
        emr.wait_screen("New chart")
        emr.fill([name, dob, sid])
        emr.key("ctrl+s")
        rid = emr.wait_screen(r"Chart (REC-\d+)").group(1)
        status = "new"
    else:
        rid = re.search(r"REC-\d+", listing.splitlines()[1]).group(0)
        log(f"  branch: existing chart {rid}")
        emr.key("ctrl+o")
        emr.wait_screen("Open chart")
        emr.type(rid)
        emr.key("Return")
        emr.wait_screen(f"Chart {rid}")
        on_screen = pane_value(emr.copy_pane(), "Subject ID")
        if on_screen != sid:
            ident["match"] = {
                "status": "ambiguous",
                "candidates": 1,
                "checked_by": EXECUTOR,
            }
            return halt(
                f"chart {rid} shows subject id {on_screen!r}, the referral has {sid!r}"
            )
        status = "existing"
    ident["fields"] = ["record_id"]
    ident["match"] = {
        "status": status,
        "record_ref": rid,
        "candidates": n,
        "checked_by": EXECUTOR,
    }

    # Write the triage entry.
    reason_code = value(req, "reason_code")
    emr.key("ctrl+t")
    emr.wait_screen(f"Triage entry {rid}")
    emr.fill([priority, reason_code, fax.name])
    emr.key("ctrl+s")
    emr.wait_screen(f"Saved {rid}")
    log(f"  wrote triage entry on {rid}: {priority}, {reason_code}, {fax.name}")

    # Committed-record read-back: close, reopen by id, read again.
    emr.key("Escape")
    emr.wait_screen("Search")
    emr.key("ctrl+o")
    emr.wait_screen("Open chart")
    emr.type(rid)
    emr.key("Return")
    emr.wait_screen(f"Chart {rid}")
    text = emr.copy_pane()
    frame = emr.frame()
    (run_dir / "frames").mkdir(exist_ok=True)
    (run_dir / "frames" / f"{fax.stem}-readback.png").write_bytes(frame.png)
    expected = {
        "record_id": ("Record", "code", rid),
        "subject_id": ("Subject ID", "code", sid),
        "priority": ("Priority", "enum", priority),
        "referral_code": ("Referral code", "code", reason_code),
        "document": ("Document", "text", fax.name),
    }
    for fname, (label, kind, want) in expected.items():
        res["fields"].append(
            read_field(
                fname,
                kind,
                expected=want,
                clipboard=pane_value(text, label),
                ocr_line=frame.value_after(label),
                frame=frame,
                session="reopened",
                artifact_id="chart",
                screen=f"Chart {rid}",
            )
        )
    res["artifacts"][0]["file_number"] = rid
    for f in res["fields"][1:]:
        log(
            f"  read back {f['name']}: {f['value']!r} [{f['grade']}]"
            + (f"  REVIEW: {f['review_reason']}" if f.get("needs_review") else "")
        )
    lost = [
        f["name"]
        for f in res["fields"][1:]
        if f.get("needs_review") and "wrote" in f.get("review_reason", "")
    ]
    if lost:
        return halt(f"read-back after reopening {rid} does not show {', '.join(lost)}")
    res["outcome"] = "committed"
    finish(req, res, run_dir, l1_dir, log)
    return "committed"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--inbox", type=Path, required=True)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--l1-dir", type=Path, required=True)
    p.add_argument("--answers", type=Path)
    a = p.parse_args()
    a.run_dir.mkdir(parents=True, exist_ok=True)
    people = People.load(a.answers, "intake_staff", "physician")
    emr = X11App("Mock EMR")
    log_path = a.run_dir / "run.log"

    def log(msg: str) -> None:
        print(msg, flush=True)
        with log_path.open("a") as fh:
            fh.write(msg + "\n")

    log("Referral triage (PROTOTYPE, synthetic data). Model calls: 0.")
    outcomes = {}
    for fax in sorted(a.inbox.glob("*.pdf")):
        try:
            outcomes[fax.name] = triage_one(emr, fax, people, a.run_dir, a.l1_dir, log)
        except DriverHalt as err:
            outcomes[fax.name] = "halted"
            log(f"  HALT (screen): {err}")
            emr.key("Escape")
    log("\nSummary: " + ", ".join(f"{k}: {v}" for k, v in outcomes.items()))


if __name__ == "__main__":
    main()
