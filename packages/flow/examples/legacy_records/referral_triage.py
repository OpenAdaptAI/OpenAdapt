"""Reference template: referral triage from a fax folder into a desktop EMR.

PROTOTYPE, SYNTHETIC DATA ONLY. A template, not part of OpenAdapt's core.

    fax folder -> parsed document -> staff confirm exceptions
      -> chart search (one match is opened) -> one physician decision
      -> EMR entry (new chart, or the existing match)
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
from typing import Any, Optional

from evidence import EXECUTOR, grade, now, read_field
from pipeline import (
    People,
    Spec,
    decide,
    finish,
    intake,
    record_no_action,
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
#: The check level a committed triage entry carries: its weakest read-back
#: grade is ``same_screen`` (the triage values are only read after
#: reopening). A ``no_action`` must show nothing was written at least as well.
COMMIT_GRADE = "same_screen"


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
    # What a save keystroke may already have created, for halt reasons.
    state: dict[str, list[str]] = {"may_exist": []}

    def halt(reason: str, *, review: bool = True) -> str:
        saved = state["may_exist"]
        reason += (
            "; may already be saved: " + ", ".join(saved)
            if saved
            else "; nothing was saved"
        )
        res["outcome"], res["halt_reason"] = "halted", reason
        log(f"  HALT: {reason}")
        finish(req, res, run_dir, l1_dir, log, review=review)
        return "halted"

    open_items = unsettled(req)
    if open_items:
        return halt(
            f"no person settled the exception on {', '.join(open_items)}; no search ran"
        )
    try:
        return _run(
            emr, fax, req, res, ident, state, people, run_dir, l1_dir, halt, log
        )
    except DriverHalt as err:
        emr.key("Escape")
        return halt(f"screen check failed: {err}")


def _search(emr: X11App, name: str, dob: str) -> tuple[int, str, Any]:
    """Search by name and date of birth; the count, the listing and a frame."""
    emr.key("Escape")
    emr.wait_screen("Search")
    emr.type(f"{name} {dob}")
    emr.key("Return")
    n = int(emr.wait_screen(r"Search results \((\d+)\)").group(1))
    return n, emr.copy_pane(), emr.frame()


def _open_chart(emr: X11App, rid: str) -> str:
    emr.key("ctrl+o")
    emr.wait_screen("Open chart")
    emr.type(rid)
    emr.key("Return")
    emr.wait_screen(f"Chart {rid}")
    return emr.copy_pane()


def _chart(
    res: dict[str, Any], ident: dict[str, Any], rid: str, pane: str, at: str, **match
) -> None:
    """Record the chart the acting session sees, and match the identity to it."""
    res["fields"].append(
        {
            "name": "chart_id",
            "type": "code",
            "value": rid,
            "confidence": 1.0,
            "reads": [
                {
                    "channel": "clipboard",
                    "value": pane_value(pane, "Record"),
                    "artifact_id": "chart",
                    "session": "acting",
                    "observed_at": at,
                }
            ],
        }
    )
    res["artifacts"][0]["file_number"] = rid
    ident["fields"] = ["chart_id"]
    ident["match"] = {"record_ref": rid, "checked_by": EXECUTOR, **match}


def _run(
    emr: X11App,
    fax: Path,
    req: dict[str, Any],
    res: dict[str, Any],
    ident: dict[str, Any],
    state: dict[str, list[str]],
    people: People,
    run_dir: Path,
    l1_dir: Path,
    halt,
    log,
) -> str:
    name, dob, sid = (
        value(req, n) for n in ("subject_name", "date_of_birth", "subject_id")
    )
    # 1. Identity, before the decision: search, and open a single match.
    n, listing, frame = _search(emr, name, dob)
    matches = read_field(
        "search_matches",
        "integer",
        expected=str(n),
        clipboard=pane_value(listing, "Matches"),
        ocr_line=frame.value_after("Matches"),
        frame=frame,
        session="acting",
        artifact_id="chart",
        screen=f"Search results ({n})",
    )
    matches["value"] = n
    res["fields"].append(matches)
    log(f"  search {name} {dob}: {n} match(es)")
    if n > 1:
        ident["match"] = {
            "status": "ambiguous",
            "candidates": n,
            "checked_by": EXECUTOR,
        }
        return halt(f"{n} charts match this name and date of birth")

    rid: Optional[str] = None
    before = ""
    before_at = ""
    if n == 1:
        rid = re.search(r"REC-\d+", listing.splitlines()[1]).group(0)
        log(f"  existing chart {rid}")
        before, before_at = _open_chart(emr, rid), now()
        if pane_value(before, "Subject ID") != sid:
            ident["match"] = {
                "status": "ambiguous",
                "candidates": 1,
                "checked_by": EXECUTOR,
            }
            return halt(
                f"chart {rid} shows subject id "
                f"{pane_value(before, 'Subject ID')!r}, the referral has {sid!r}"
            )
        _chart(res, ident, rid, before, before_at, status="existing", candidates=1)

    # 2. The decision.
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
        return _decline(
            emr,
            req,
            res,
            ident,
            matches,
            rid,
            before,
            before_at,
            name,
            dob,
            people,
            run_dir,
            l1_dir,
            halt,
            log,
        )

    # 3. Write: a new chart if none matched, then the triage entry.
    if n == 0:
        log("  branch: new chart")
        emr.key("ctrl+n")
        emr.wait_screen("New chart")
        emr.fill([name, dob, sid])
        state["may_exist"].append(f"a new chart for {name}")
        emr.key("ctrl+s")
        rid = emr.wait_screen(r"Chart (REC-\d+)").group(1)
        state["may_exist"][-1] = f"new chart {rid}"
        before, before_at = emr.copy_pane(), now()
        if pane_value(before, "Subject ID") != sid:
            ident["match"] = {
                "status": "ambiguous",
                "candidates": 1,
                "checked_by": EXECUTOR,
            }
            return halt(
                f"new chart {rid} shows subject id "
                f"{pane_value(before, 'Subject ID')!r}, the referral has {sid!r}"
            )
        _chart(res, ident, rid, before, before_at, status="new", candidates=0)
    assert rid is not None
    entries_before = pane_value(before, "Triage entries")
    if entries_before is None or not entries_before.isdigit():
        return halt(f"chart {rid} does not show its triage entry count")

    reason_code = value(req, "reason_code")
    emr.key("ctrl+t")
    emr.wait_screen(f"Triage entry {rid}")
    emr.fill([priority, reason_code, fax.name])
    state["may_exist"].append(f"a triage entry on {rid}")
    emr.key("ctrl+s")
    emr.wait_screen(f"Saved {rid}")
    log(f"  wrote triage entry on {rid}: {priority}, {reason_code}, {fax.name}")

    # Committed-record read-back: close, reopen by id, read again.
    emr.key("Escape")
    emr.wait_screen("Search")
    text = _open_chart(emr, rid)
    frame = emr.frame()
    (run_dir / "frames").mkdir(exist_ok=True)
    (run_dir / "frames" / f"{fax.stem}-readback.png").write_bytes(frame.png)
    # record_id and subject_id were also read before the write, in the acting
    # session; the triage values were only typed into a form there, so they
    # have reads of the reopened record only.
    expected = {
        "record_id": ("Record", "code", rid, pane_value(before, "Record")),
        "subject_id": ("Subject ID", "code", sid, pane_value(before, "Subject ID")),
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
            acting=[("clipboard", acting, before_at)] if acting is not None else None,
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
                    "observed_at": now(),
                }
            ],
        }
    )
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


def _decline(
    emr: X11App,
    req: dict[str, Any],
    res: dict[str, Any],
    ident: dict[str, Any],
    matches: dict[str, Any],
    rid: Optional[str],
    before: str,
    before_at: str,
    name: str,
    dob: str,
    people: People,
    run_dir: Path,
    l1_dir: Path,
    halt,
    log,
) -> str:
    """A declined referral writes nothing. Read back, after the decision, the
    state read before it: the search result, and for one chart its triage
    entry count after reopening it."""
    n = matches["value"]
    n_after, listing, frame = _search(emr, name, dob)
    matches["reads"] += read_field(
        "x",
        "integer",
        expected=str(n),
        clipboard=pane_value(listing, "Matches"),
        ocr_line=frame.value_after("Matches"),
        frame=frame,
        session="reopened",
        artifact_id="chart",
        screen=f"Search results ({n_after})",
    )["reads"]
    matches["grade"] = grade(matches["reads"], str(n))
    if n_after != n:
        return halt(
            f"the search found {n} charts before the decision and {n_after} after"
        )
    evidence = ["search_matches"]
    if n == 0:
        ident["match"] = {"status": "new", "candidates": 0, "checked_by": EXECUTOR}
    else:
        assert rid is not None
        entries_before = pane_value(before, "Triage entries")
        text = _open_chart(emr, rid)
        frame = emr.frame()
        entries = read_field(
            "triage_entries",
            "integer",
            expected=entries_before,
            clipboard=pane_value(text, "Triage entries"),
            ocr_line=frame.value_after("Triage entries"),
            frame=frame,
            session="reopened",
            artifact_id="chart",
            screen=f"Chart {rid}",
            acting=[("clipboard", entries_before, before_at)],
        )
        after = entries["value"]
        if entries_before is None or not entries_before.isdigit():
            return halt(f"chart {rid} does not show its triage entry count")
        if after != entries_before:
            return halt(
                f"chart {rid} had {entries_before} triage entries before the "
                f"decision and {after} after reopening"
            )
        # The evidence value is the state read before the decision.
        entries["value"] = int(entries_before)
        res["fields"].append(entries)
        evidence.append("triage_entries")
        log(f"  triage entries on {rid}: {entries_before}, unchanged after reopening")
    weak = record_no_action(
        req, res, reason_code="declined", required_grade=COMMIT_GRADE, evidence=evidence
    )
    if weak:
        return halt(weak)
    log("  no_action: declined, nothing written")
    finish(req, res, run_dir, l1_dir, log, review=people.review_decisions)
    return "declined"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--inbox", type=Path, required=True)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--l1-dir", type=Path, required=True)
    p.add_argument("--answers", type=Path)
    p.add_argument(
        "--review-decisions",
        action="store_true",
        help="site policy: send declined referrals (no_action) to review",
    )
    a = p.parse_args()
    a.run_dir.mkdir(parents=True, exist_ok=True)
    people = People.load(a.answers, "intake_staff", "physician")
    people.review_decisions = people.review_decisions or a.review_decisions
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
