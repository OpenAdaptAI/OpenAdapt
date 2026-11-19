"""Record, compile and replay each dropdown and popup family; score every run.

For each widget page of :mod:`app` the harness records one demonstration the
way a person makes it (real pointer and keyboard input in headless Chromium,
through :func:`record_interactive`), compiles it, then replays the bundle in
fresh browsers under a fixed set of cases:

``demo``         the demonstrated choice again, a new patient name
``heldout``      a different choice, passed as the field's parameter
``reorder_late`` the demonstrated choice, options reversed and 800 ms late
``missing``      the demonstrated option is gone (a correct run stops)
``duplicate``    two options show the demonstrated label (a correct run stops)
``disabled``     the demonstrated option is disabled (a correct run stops)
``surprise_confirm`` an extra browser ``confirm()`` the demonstration never
                 saw (a correct run stops)
``surprise_modal`` a notice dialog over the form (a correct run stops or
                 closes it only with a recorded step)

Each run is scored from the app's stored records, never from the run report
alone:

``verified``   the run succeeded and the stored record has the expected values
``safe_halt``  the run stopped and nothing wrong was stored
``wrong``      a record with a wrong value was stored, or the run claimed
               success without the expected record
``unbindable`` the bundle has no parameter for the field, so the job cannot
               be expressed (held-out case only)
``record_refused`` / ``compile_refused`` the pipeline refused before replay

Usage::

    python -m benchmark.choice_widgets.measure --out results/<label>.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmark.choice_widgets.app import ChoiceApp, Server  # noqa: E402

DEMO_NAME = "Asha Kumar"
CASES = (
    "demo",
    "heldout",
    "reorder_late",
    "missing",
    "duplicate",
    "disabled",
    "surprise_confirm",
    "surprise_modal",
)
#: case -> (query string, expected field values or None when a stop is right)
_CASE_QUERY = {
    "demo": "",
    "heldout": "",
    "reorder_late": "order=rev&delay=800",
    "missing": "missing={code}",
    "duplicate": "dup={code}",
    "disabled": "disabled={code}",
    "surprise_confirm": "surprise=confirm",
    "surprise_modal": "surprise=modal",
}
_MUST_STOP = {"missing", "duplicate", "disabled", "surprise_confirm", "surprise_modal"}


def _settle(pump: Callable[[], Any], n: int = 10) -> None:
    for _ in range(n):
        pump()


def _type_name(page: Any, pump: Callable[[], Any]) -> None:
    page.locator("#patient_name").click()
    _settle(pump)
    page.keyboard.type(DEMO_NAME)
    _settle(pump)


def _save(page: Any, pump: Callable[[], Any]) -> None:
    page.locator("#save").click()
    _settle(pump, 30)
    page.wait_for_selector("#records:has-text('Last saved')", timeout=10000)
    _settle(pump, 10)


def _native(page: Any, pump: Callable[[], Any], selector: str, label: str) -> None:
    # The browser draws a native select's list outside the page, so the
    # page sees a click on the control, the keys, and then a change. A person
    # picking with the mouse produces the same click and change.
    page.locator(selector).click()
    _settle(pump)
    page.keyboard.type(label)
    page.keyboard.press("Enter")
    _settle(pump)


def drive(kind: str) -> Callable[[Any, Callable[[], Any]], None]:
    """A person's demonstration of the widget page ``kind``."""

    def run(page: Any, pump: Callable[[], Any]) -> None:
        # The person answers OK to every browser dialog.
        page.on("dialog", lambda dialog: dialog.accept())
        page.wait_for_selector("#patient_name", state="visible")
        _settle(pump)
        if kind == "register":
            _drive_register(page, pump)
            return
        _type_name(page, pump)
        if kind == "native":
            _native(page, pump, "#department", "Cardiology")
        elif kind == "aria_listbox":
            page.locator("#department-btn").click()
            _settle(pump)
            page.locator("[role=option]", has_text="Cardiology").click()
            _settle(pump)
        elif kind == "div_dropdown":
            page.locator(".dd").click()
            _settle(pump)
            page.locator(".dd-item", has_text="Cardiology").click()
            _settle(pump)
        elif kind == "select2":
            page.locator(".select2-selection").click()
            _settle(pump)
            page.keyboard.type("Card")
            _settle(pump)
            page.locator(".select2-results__option", has_text="Cardiology").click()
            _settle(pump)
        elif kind == "chosen":
            page.locator(".chosen-single").click()
            _settle(pump)
            page.keyboard.type("Card")
            _settle(pump)
            page.locator(".chosen-results li", has_text="Cardiology").click()
            _settle(pump)
        elif kind == "tom_select":
            page.locator(".ts-control").click()
            _settle(pump)
            page.keyboard.type("Card")
            page.wait_for_selector(".ts-dropdown .option", timeout=10000)
            _settle(pump)
            page.locator(".ts-dropdown .option", has_text="Cardiology").click()
            _settle(pump)
        elif kind == "async_combobox":
            page.locator("#department-input").click()
            _settle(pump)
            page.keyboard.type("Card")
            page.wait_for_selector("#department-listbox [role=option]", timeout=10000)
            _settle(pump)
            page.locator(
                "#department-listbox [role=option]", has_text="Cardiology"
            ).click()
            _settle(pump)
        elif kind == "cascade":
            _native(page, pump, "#department", "Cardiology")
            page.wait_for_selector("#doctor:not([disabled])", timeout=10000)
            _settle(pump)
            _native(page, pump, "#doctor", "Dr. Asha Rao")
        elif kind == "datepicker":
            page.locator("#visit_date").click()
            _settle(pump)
            page.locator(
                ".flatpickr-calendar.open .flatpickr-day:not(.prevMonthDay)"
                ":not(.nextMonthDay)",
                has_text="15",
            ).first.click()
            _settle(pump)
        else:
            raise KeyError(kind)
        _save(page, pump)

    return run


#: The registration task: parameter -> (demonstrated label, value) and the
#: held-out job's (label, value).
REGISTER: dict[str, tuple[tuple[str, str], tuple[str, str]]] = {
    "title": (("Mr", "MR"), ("Mrs", "MRS")),
    "gender": (("Male", "M"), ("Female", "F")),
    "blood_group": (("B+", "BP"), ("O-", "ON")),
    "department": (("Cardiology", "CARD"), ("Orthopaedics", "ORTH")),
    "doctor": (("Dr. Asha Rao", "D11"), ("Dr. Kiran Menon", "D21")),
    "city": (("Bengaluru", "BLR"), ("Mysuru", "MYS")),
    "followup_in": (("1 week", "1W"), ("1 month", "1M")),
}


def _drive_register(page: Any, pump: Callable[[], Any]) -> None:
    """A registration and its follow-up job, every family on one page."""

    _native(page, pump, "#title", "Mr")
    _type_name(page, pump)
    page.locator("#gender-btn").click()
    _settle(pump)
    page.locator("#gender-listbox").get_by_text("Male", exact=True).click()
    _settle(pump)
    page.locator(".dd").click()
    _settle(pump)
    page.locator(".dd-menu").get_by_text("B+", exact=True).click()
    _settle(pump)
    page.locator(".select2-selection").click()
    _settle(pump)
    page.keyboard.type("Card")
    _settle(pump)
    page.locator(".select2-results__option", has_text="Cardiology").click()
    _settle(pump)
    page.wait_for_selector("#doctor:not([disabled])", timeout=10000)
    _settle(pump)
    _native(page, pump, "#doctor", "Dr. Asha Rao")
    page.locator("#city-input").click()
    _settle(pump)
    page.keyboard.type("Beng")
    page.wait_for_selector("#city-listbox [role=option]", timeout=10000)
    _settle(pump)
    page.locator("#city-listbox [role=option]", has_text="Bengaluru").click()
    _settle(pump)
    page.locator("#visit_date").click()
    _settle(pump)
    page.locator(
        ".flatpickr-calendar.open .flatpickr-day:not(.prevMonthDay):not(.nextMonthDay)",
        has_text="15",
    ).first.click()
    _settle(pump)
    _save(page, pump)
    page.locator("#followup").click()
    _settle(pump, 15)
    _native(page, pump, "#fu_in", "1 week")
    page.locator("#fu-ok").click()
    _settle(pump, 30)
    page.wait_for_selector("#toast:has-text('Follow-up booked')", timeout=10000)
    _settle(pump, 10)


#: kind -> (field, demonstrated value, held-out label, held-out value)
EXPECT: dict[str, tuple[str, str, str, str]] = {
    "native": ("department", "CARD", "Orthopaedics", "ORTH"),
    "aria_listbox": ("department", "CARD", "Orthopaedics", "ORTH"),
    "div_dropdown": ("department", "CARD", "Orthopaedics", "ORTH"),
    "select2": ("department", "CARD", "Orthopaedics", "ORTH"),
    "chosen": ("department", "CARD", "Orthopaedics", "ORTH"),
    "tom_select": ("department", "CARD", "Orthopaedics", "ORTH"),
    "async_combobox": ("department", "CARD", "Orthopaedics", "ORTH"),
    "cascade": ("department", "CARD", "Orthopaedics", "ORTH"),
    "datepicker": ("visit_date", "", "", ""),
    "register": ("department", "CARD", "Orthopaedics", "ORTH"),
}


def _path(kind: str) -> str:
    return "/register" if kind == "register" else f"/w/{kind}"


def _record(server: Server, kind: str, root: Path) -> tuple[Optional[Path], str]:
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.interactive_recorder import record_interactive

    field = EXPECT[kind][0]
    params = ["patient_name", field]
    if kind == "cascade":
        params.append("doctor")
    if kind == "register":
        params = ["patient_name", *REGISTER]
    try:
        rec = record_interactive(
            f"{server.base}{_path(kind)}",
            root / "rec",
            headless=True,
            script=drive(kind),
            param_fields=tuple(params),
        )
    except Exception as exc:  # noqa: BLE001 - scored, not raised
        return None, f"record_refused: {type(exc).__name__}: {str(exc)[:300]}"
    try:
        compile_recording(rec, root / "bundle", name=f"register_{kind}")
    except Exception as exc:  # noqa: BLE001
        return None, f"compile_refused: {type(exc).__name__}: {str(exc)[:300]}"
    return root / "bundle", "ok"


def _replay(
    server: Server,
    kind: str,
    bundle: Path,
    query: str,
    params: dict[str, str],
    run_dir: Path,
) -> tuple[bool, str]:
    from playwright.sync_api import sync_playwright

    from openadapt_flow.backends.playwright_backend import (
        PlaywrightBackend,
        prepare_ws_guard,
    )
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            prepare_ws_guard(page.context)
            page.goto(f"{server.base}{_path(kind)}" + (f"?{query}" if query else ""))
            page.wait_for_selector("#patient_name", state="visible")
            page.wait_for_timeout(300)
            report = Replayer(PlaywrightBackend(page)).run(
                Workflow.load(bundle), params=params, bundle_dir=bundle, run_dir=run_dir
            )
            page.wait_for_timeout(1500)
        finally:
            browser.close()
    detail = ""
    if not report.success:
        failed = [s for s in report.results if getattr(s, "error", None)]
        detail = (
            f"{failed[-1].step_id}: {failed[-1].error}"[:400]
            if failed
            else str(report.terminal_outcome or "failed")
        )
    return bool(report.success), detail


def measure_kind(server: Server, kind: str, work: Path) -> dict[str, Any]:
    from openadapt_flow.ir import Workflow

    field, demo_value, held_label, held_value = EXPECT[kind]
    out: dict[str, Any] = {"kind": kind, "cases": {}}
    bundle, status = _record(server, kind, work / kind)
    out["pipeline"] = status
    if bundle is None:
        category = status.split(":", 1)[0]
        for case in CASES:
            out["cases"][case] = {"score": category}
        return out
    workflow = Workflow.load(bundle)
    declared = set(workflow.params)
    out["params"] = sorted(declared)
    out["steps"] = [
        {"action": s.action.value, "intent": s.intent[:80]} for s in workflow.steps
    ]
    code = demo_value
    for case in CASES:
        if kind == "datepicker" and case in {
            "heldout",
            "missing",
            "duplicate",
            "disabled",
        }:
            out["cases"][case] = {"score": "n/a"}
            continue
        if case == "heldout" and field not in declared:
            out["cases"][case] = {"score": "unbindable"}
            continue
        params = {"patient_name": f"Case {case} {kind}"}
        expected = demo_value
        if kind == "register" and case == "heldout":
            for name, (_demo, (label, _value)) in REGISTER.items():
                params[name] = label
        elif case == "heldout":
            params[field] = held_label
            expected = held_value
            if kind == "cascade":
                params["doctor"] = "Dr. Kiran Menon"
        if kind == "register" and case == "heldout":
            missing = [name for name in REGISTER if name not in declared]
            if missing:
                out["cases"][case] = {
                    "score": "unbindable",
                    "detail": ",".join(missing),
                }
                continue
        params = {k: v for k, v in params.items() if k in declared}
        query = _CASE_QUERY[case].format(code=code)
        before = len(server.app.records)
        started = time.monotonic()
        try:
            success, detail = _replay(
                server, kind, bundle, query, params, work / kind / f"run-{case}"
            )
        except Exception as exc:  # noqa: BLE001
            success, detail = False, f"replay error: {type(exc).__name__}: {exc}"[:300]
        records = server.app.records[before:]
        score = _score(kind, case, success, records, field, expected, params)
        out["cases"][case] = {
            "score": score,
            "run_success": success,
            "records": records,
            "detail": detail,
            "seconds": round(time.monotonic() - started, 1),
        }
    return out


def _score(
    kind: str,
    case: str,
    success: bool,
    records: list[dict[str, Any]],
    field: str,
    expected: str,
    params: dict[str, str],
) -> str:
    if kind == "register":
        return _score_register(case, success, records, params)

    def correct(record: dict[str, Any]) -> bool:
        if record.get("patient_name") != params.get("patient_name", DEMO_NAME):
            return False
        if kind == "datepicker":
            return bool(record.get("visit_date"))
        if record.get(field) != expected:
            return False
        if kind == "cascade":
            want = "D21" if case == "heldout" else "D11"
            return record.get("doctor") == want
        return True

    if case in _MUST_STOP:
        if not records:
            return "safe_halt" if not success else "wrong"
        # A stop case may still save correctly if the program handled it
        # with a recorded step; any other stored value is wrong.
        return "verified" if (success and all(correct(r) for r in records)) else "wrong"
    if len(records) == 1 and correct(records[0]):
        return "verified" if success else "safe_halt"
    if not records:
        return "safe_halt" if not success else "wrong"
    return "wrong"


def _score_register(
    case: str, success: bool, records: list[dict[str, Any]], params: dict[str, str]
) -> str:
    held = case == "heldout"
    want = {name: (h if held else d)[1] for name, (d, h) in REGISTER.items()}
    regs = [r for r in records if r.get("kind") == "registration"]
    follow = [r for r in records if r.get("kind") == "followup"]
    reg_ok = (
        len(regs) == 1
        and regs[0].get("patient_name") == params.get("patient_name")
        and all(regs[0].get(k) == v for k, v in want.items() if k != "followup_in")
        and bool(regs[0].get("visit_date"))
    )
    follow_ok = (
        len(follow) == 1
        and follow[0].get("followup_in") == want["followup_in"]
        and regs
        and follow[0].get("registration") == regs[0].get("id")
    )
    if not records:
        return "wrong" if success else "safe_halt"
    if reg_ok and follow_ok and len(records) == 2:
        return "verified" if success else "safe_halt"
    if reg_ok and not follow and not success:
        # The registration is right and the run stopped before the follow-up.
        return "safe_halt"
    return "wrong"


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    table: dict[str, dict[str, str]] = {}
    totals: dict[str, int] = {}
    for item in results:
        row = {case: item["cases"][case]["score"] for case in CASES}
        table[item["kind"]] = row
        for score in row.values():
            totals[score] = totals.get(score, 0) + 1
    return {"table": table, "totals": totals}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--kinds", default="")
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)
    from benchmark.choice_widgets.app import WIDGETS

    kinds = args.kinds.split(",") if args.kinds else [*WIDGETS, "register"]
    server = ChoiceApp().serve()
    results: list[dict[str, Any]] = []
    try:
        with tempfile.TemporaryDirectory(prefix="choice-widgets-") as tmp:
            for kind in kinds:
                try:
                    results.append(measure_kind(server, kind, Path(tmp)))
                except Exception:  # noqa: BLE001
                    results.append(
                        {
                            "kind": kind,
                            "error": traceback.format_exc()[-1500:],
                            "cases": {c: {"score": "harness_error"} for c in CASES},
                        }
                    )
                print(
                    json.dumps({kind: summarize(results[-1:])["table"].get(kind)}),
                    flush=True,
                )
    finally:
        server.close()
    payload = {"label": args.label, "results": results, "summary": summarize(results)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
