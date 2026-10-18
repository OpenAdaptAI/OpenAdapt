"""Reference template: referral triage from a fax folder into a desktop EMR.

PROTOTYPE, SYNTHETIC DATA ONLY. A template, not part of OpenAdapt's core.

    fax folder -> parsed document -> staff confirm exceptions
      -> one physician decision -> EMR entry (new chart, or existing match)
      -> close, reopen by record id, read back -> Seal -> L1 directory

A name-and-date-of-birth search that finds more than one chart stops the run
and puts the referral in the review queue. Nothing is written for it. After
the save, the chart is reopened and must show one more triage entry than
before, so a save the app reports but drops halts even when an earlier entry
with the same values is on screen. A screen that does not appear as expected,
before or after the save, also halts with a result exchange.

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
from pipeline import (
    People,
    Spec,
    decide,
    finish,
    intake,
    result_exchange,
    unsettled,
)
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
    file_number = sid or fax.stem
    req["artifacts"][0]["file_number"] = file_number
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
    res = result_exchange(
        req,
        artifact={
            "artifact_id": "chart",
            "source_type": "rdp_screen",
            "file_number": file_number,
            "date": today,
            "doctype": "triage-entry",
        },
    )
    # Until a chart is matched, the identity is the subject id the request
    # carries; no application read is claimed for it.
    res["fields"].append(
        {
            "name": "referral_subject_id",
            "type": "code",
            "value": sid,
            "confidence": 1.0,
        }
    )
    ident = {
        "identity_id": "subject",
        "role": "subject",
        "fields": ["referral_subject_id"],
        "match": {"status": "not_checked"},
    }
    res["identities"] = [ident]
    state = {"saved": False}

    def halt(reason: str, *, review: bool = True) -> str:
        res["outcome"], res["halt_reason"] = "halted", reason
        log(f"  HALT: {reason}")
        finish(req, res, run_dir, l1_dir, log, review=review)
        return "halted"

    open_items = unsettled(req)
    if open_items:
        return halt(
            f"no person settled the exception on {', '.join(open_items)}; "
            "no search ran and nothing was written"
        )

    priority = decide(
        req,
        people,
        question="What priority should this referral get?",
        options=PRIORITIES,
        role="physician",
        output_field="priority",
        log=log,
    )
    if priority == "decline":
        # Nothing to do in the EMR: no search ran, so no match is claimed.
        res["outcome"] = "halted"
        res["halt_reason"] = (
            "declined by the physician; no chart search ran and nothing was written"
        )
        log("  declined: no search, nothing written")
        finish(req, res, run_dir, l1_dir, log, review=False)
        return "declined"

    try:
        return _write_and_read_back(
            emr,
            fax,
            req,
            res,
            ident,
            state,
            name,
            dob,
            sid,
            priority,
            run_dir,
            l1_dir,
            halt,
            log,
        )
    except DriverHalt as err:
        when = (
            "after the save keystroke, so the entry may or may not be saved"
            if state["saved"]
            else "before the save keystroke; nothing was saved"
        )
        emr.key("Escape")
        return halt(f"screen check failed {when}: {err}")


def _write_and_read_back(
    emr: X11App,
    fax: Path,
    req: dict[str, Any],
    res: dict[str, Any],
    ident: dict[str, Any],
    state: dict[str, bool],
    name: str,
    dob: str,
    sid: str,
    priority: str,
    run_dir: Path,
    l1_dir: Path,
    halt,
    log,
) -> str:
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
        status = "existing"

    # The chart as the acting session sees it before the write.
    before = emr.copy_pane()
    acting_rid = pane_value(before, "Record")
    acting_sid = pane_value(before, "Subject ID")
    entries_before = pane_value(before, "Triage entries")
    if acting_sid != sid:
        ident["match"] = {
            "status": "ambiguous",
            "candidates": max(n, 1),
            "checked_by": EXECUTOR,
        }
        return halt(
            f"chart {rid} shows subject id {acting_sid!r}, the referral has {sid!r}"
        )
    if entries_before is None or not entries_before.isdigit():
        return halt(
            f"chart {rid} does not show its triage entry count; nothing was written"
        )
    res["fields"].append(
        {
            "name": "chart_id",
            "type": "code",
            "value": rid,
            "confidence": 1.0,
            "reads": [
                {
                    "channel": "clipboard",
                    "value": acting_rid,
                    "artifact_id": "chart",
                    "session": "acting",
                }
            ],
        }
    )
    ident["fields"] = ["chart_id"]
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
    state["saved"] = True
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
    # record_id and subject_id were also read before the write, in the acting
    # session; the triage values were only typed into a form there, so they
    # have reads of the reopened record only.
    expected = {
        "record_id": ("Record", "code", rid, acting_rid),
        "subject_id": ("Subject ID", "code", sid, acting_sid),
        "priority": ("Priority", "enum", priority, None),
        "referral_code": ("Referral code", "code", reason_code, None),
        "document": ("Document", "text", fax.name, None),
    }
    readback = []
    for fname, (label, kind, want, acting) in expected.items():
        f = read_field(
            fname,
            kind,
            expected=want,
            clipboard=pane_value(text, label),
            ocr_line=frame.value_after(label),
            frame=frame,
            session="reopened",
            artifact_id="chart",
            screen=f"Chart {rid}",
            acting=[("clipboard", acting)] if acting is not None else None,
        )
        res["fields"].append(f)
        readback.append(f)
    # A save the app reported but dropped can leave an earlier entry with the
    # same values on screen, so the entry count must have gone up by one.
    entries_after = pane_value(text, "Triage entries")
    res["fields"].append(
        {
            "name": "triage_entries",
            "type": "integer",
            "value": int(entries_after)
            if entries_after and entries_after.isdigit()
            else None,
            "confidence": 1.0,
            "reads": [
                {
                    "channel": "clipboard",
                    "value": entries_after,
                    "artifact_id": "chart",
                    "session": "reopened",
                }
            ],
        }
    )
    res["artifacts"][0]["file_number"] = rid
    for f in readback:
        log(
            f"  read back {f['name']}: {f['value']!r} [{f['grade']}]"
            + (f"  REVIEW: {f['review_reason']}" if f.get("needs_review") else "")
        )
    log(f"  triage entries on {rid}: {entries_before} before, {entries_after} after")
    lost = [
        f["name"]
        for f in readback
        if f.get("needs_review") and "wrote" in f.get("review_reason", "")
    ]
    if lost:
        return halt(f"read-back after reopening {rid} does not show {', '.join(lost)}")
    if entries_after != str(int(entries_before) + 1):
        return halt(
            f"chart {rid} had {entries_before} triage entries before the save and "
            f"{entries_after} after reopening; the new entry was not kept"
        )
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
