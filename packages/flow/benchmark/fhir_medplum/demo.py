"""Teach Medplum's new-patient form: a scripted demonstration, then compile.

    PYTHONPATH=. python -m benchmark.fhir_medplum.demo record --out DIR [--values FILE]
    PYTHONPATH=. python -m benchmark.fhir_medplum.demo compile --recording DIR --bundle DIR

Signing in and opening the blank form are setup, not part of the
demonstration (``qualify_hooks.open_session``). The demonstration fills the
form and clicks Create. Every action goes through the ``Recorder``, so the
frames and events are what a person's demonstration would leave; record time
uses Playwright locators only to find where to click.

Three choices the form forces:

- Gender is an autocomplete over a ValueSet: the clerk types the code
  (``female``), then ArrowDown and Enter take the exact match. Enter alone
  submits the whole form.
- Telecom's system is a native select: the clerk types the option
  (``phone``, ``email``) into it.
- Controls with nothing readable beside them are reached by keyboard (see
  ``demonstrate``).
- Birth Date is a native date input, typed as ``date_keys`` gives it.

Synthetic values only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

#: The demonstration's patient (synthetic). Replays bring their own values.
DEFAULT_VALUES = {
    "fname": "Rowan",
    "lname": "Achterberg",
    "DOB": "1979-11-23",
    "sex": "female",
    "street": "41 Quarry Lane",
    "city": "Worcester",
    "state_label": "Massachusetts",
    "postal_code": "01609",
    "phone_home": "508-555-0147",
    "email": "rowan.achterberg@example.invalid",
}

SETTLE = {
    "settle_timeout_s": 10.0,
    "settle_stable_frames": 3,
    "settle_interval_s": 0.3,
}

#: Targets are kept below the sticky header and banner, above the fold.
SAFE_TOP, SAFE_BOTTOM, ANCHOR_Y = 170, 800, 420


def date_keys(iso: str) -> str:
    """The keystrokes headless Chromium's date input takes for an ISO date.

    It shows the segments year first, and the year takes six digits before
    the caret moves on, so 1979-11-23 is typed 0019791123.
    """

    year, month, day = iso.split("-")
    return f"{int(year):06d}{month}{day}"


def _center(recorder: Any, page: Any, locator: Any) -> tuple[int, int]:
    """Bring ``locator`` into the safe band with recorded wheel scrolls."""

    last = None
    for _ in range(6):
        box = locator.bounding_box()
        if box is None:
            raise RuntimeError("target is not on the page")
        mid = box["y"] + box["height"] / 2
        if SAFE_TOP <= mid <= SAFE_BOTTOM:
            return int(box["x"] + box["width"] / 2), int(mid)
        if last is not None and abs(mid - last) < 1 and SAFE_TOP <= mid <= 890:
            # The page can't scroll further (the Create button is last).
            return int(box["x"] + box["width"] / 2), int(mid)
        last = mid
        recorder.scroll(0, int(mid - ANCHOR_Y))
        page.wait_for_timeout(400)
    raise RuntimeError("could not bring the target into view")


def _click(recorder: Any, page: Any, locator: Any) -> None:
    recorder.click(*_center(recorder, page, locator))
    page.wait_for_timeout(250)


_FOCUSED = (
    "() => { const e = document.activeElement; return [e.tagName,"
    " e.getAttribute('name') || '', e.getAttribute('data-testid') || '',"
    " e.getAttribute('placeholder') || '', (e.innerText || '').trim().slice(0, 30)]; }"
)


def _keys(
    recorder: Any, page: Any, key: str, times: int, expect: dict[str, str]
) -> None:
    """Move the focus by keyboard, then check where it landed (not recorded)."""

    for _ in range(times):
        recorder.press(key)
        page.wait_for_timeout(120)
    tag, name, testid, placeholder, text = page.evaluate(_FOCUSED)
    got = {
        "tag": tag,
        "name": name,
        "testid": testid,
        "placeholder": placeholder,
        "text": text,
    }
    wrong = {k: (v, got[k]) for k, v in expect.items() if got[k] != v}
    if wrong:
        raise RuntimeError(f"focus landed elsewhere after {times} x {key}: {wrong}")


def _keys_until(
    recorder: Any, page: Any, key: str, expect: dict[str, str], most: int = 4
) -> int:
    """Press ``key`` until the focus matches ``expect``; the presses are recorded."""

    for n in range(1, most + 1):
        recorder.press(key)
        page.wait_for_timeout(120)
        tag, name, testid, placeholder, text = page.evaluate(_FOCUSED)
        got = {
            "tag": tag,
            "name": name,
            "testid": testid,
            "placeholder": placeholder,
            "text": text,
        }
        if all(got.get(k) == v for k, v in expect.items()):
            return n
    raise RuntimeError(f"focus never reached {expect} in {most} x {key}")


def demonstrate(recorder: Any, page: Any, values: dict[str, str]) -> None:
    """Fill the new-patient form once and click Create.

    Medplum's generic resource form gives several controls nothing to read
    beside them: the "Add Name" button, the Telecom selects and value fields,
    Gender and Birth Date. A click on them can't carry an identity check, and
    clinical-write refuses an unarmed click. So the demonstration reaches
    them by keyboard from a click that does carry one, as a keyboard user
    would; every keystroke is recorded, and record time checks the focus.
    """

    # The first control has nothing beside it, so the demonstration tabs
    # from the top of the page to "Add Name" (Space presses a button).
    _keys_until(recorder, page, "Tab", {"tag": "BUTTON", "text": "Add Name"}, most=15)
    recorder.press("Space")
    page.wait_for_timeout(300)
    _click(recorder, page, page.get_by_placeholder("Given"))
    recorder.type_text(values["fname"], param="fname")
    _click(recorder, page, page.get_by_placeholder("Family"))
    recorder.type_text(values["lname"], param="lname")

    # Each Telecom row: system, use, value, remove, then the Add button.
    for system, param in (("phone", "phone_home"), ("email", "email")):
        _click(recorder, page, page.get_by_role("button", name="Add Telecom"))
        _keys(recorder, page, "Shift+Tab", 4, {"tag": "SELECT", "testid": "system"})
        recorder.type_text(system)
        _keys(recorder, page, "Tab", 2, {"tag": "INPUT", "placeholder": "Value"})
        recorder.type_text(values[param], param=param)

    # Birth Date: the row's remove button, Add Telecom, Gender, Birth Date.
    # Choosing a gender re-renders the field and drops the focus, so the date
    # goes first and Gender last.
    _keys(recorder, page, "Tab", 4, {"tag": "INPUT", "name": "birthDate"})
    recorder.type_text(date_keys(values["DOB"]), param="DOB")
    _keys_until(recorder, page, "Shift+Tab", {"tag": "INPUT", "name": "gender"})
    recorder.type_text(values["sex"], param="sex")
    page.wait_for_timeout(1200)
    recorder.press("ArrowDown")
    recorder.press("Enter")
    page.wait_for_timeout(300)

    _click(recorder, page, page.get_by_role("button", name="Add Address"))
    _click(recorder, page, page.get_by_placeholder("Line 1"))
    recorder.type_text(values["street"], param="street")
    _click(recorder, page, page.get_by_placeholder("City"))
    recorder.type_text(values["city"], param="city")
    _click(recorder, page, page.get_by_placeholder("State"))
    recorder.type_text(values["state_label"], param="state_label")
    _click(recorder, page, page.get_by_placeholder("Postal Code"))
    recorder.type_text(values["postal_code"], param="postal_code")

    _click(recorder, page, page.get_by_role("button", name="Create"))
    page.wait_for_url(lambda url: "/Patient/new" not in url, timeout=30_000)
    page.wait_for_timeout(1500)


def cmd_record(args: argparse.Namespace) -> int:
    from types import SimpleNamespace

    from benchmark.fhir_medplum import qualify_hooks
    from openadapt_flow.recorder import Recorder

    out = Path(args.out)
    if out.exists():
        raise SystemExit(f"recording path already exists: {out}")
    values = dict(DEFAULT_VALUES)
    if args.values:
        values.update(json.loads(Path(args.values).read_text()))
    config = SimpleNamespace(app=SimpleNamespace(viewport=(1280, 900)))
    backend, close = qualify_hooks.open_session(config)
    try:
        recorder = Recorder(backend, out, app_url=backend.page.url, **SETTLE)
        demonstrate(recorder, backend.page, values)
        print("recording:", recorder.finish())
        print("saved:", backend.page.url)
    finally:
        close()
    return 0


def cmd_compile(args: argparse.Namespace) -> int:
    from openadapt_flow.compiler import compile_recording

    bundle = Path(args.bundle)
    if bundle.exists():
        raise SystemExit(f"bundle path already exists: {bundle}")
    workflow = compile_recording(
        Path(args.recording), bundle, name="medplum-new-patient"
    )
    print("bundle:", bundle, "steps:", len(workflow.steps))
    print("params:", sorted(workflow.params) if workflow.params else [])
    return 0


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("record")
    p.add_argument("--out", required=True)
    p.add_argument("--values", default=None, help="JSON file of field values")
    p.set_defaults(func=cmd_record)
    p = sub.add_parser("compile")
    p.add_argument("--recording", required=True)
    p.add_argument("--bundle", required=True)
    p.set_defaults(func=cmd_compile)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
