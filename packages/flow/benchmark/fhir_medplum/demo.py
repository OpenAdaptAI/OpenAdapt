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
- Telecom's system is a native select: the clerk clicks it and types the
  option (``phone``, ``email``).
- Birth Date is a native date input; the backend sets an ISO date into it.

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


def demonstrate(recorder: Any, page: Any, values: dict[str, str]) -> None:
    """Fill the new-patient form once and click Create."""

    _click(recorder, page, page.get_by_role("button", name="Add Name"))
    _click(recorder, page, page.get_by_placeholder("Given"))
    recorder.type_text(values["fname"], param="fname")
    _click(recorder, page, page.get_by_placeholder("Family"))
    recorder.type_text(values["lname"], param="lname")

    _click(recorder, page, page.get_by_role("button", name="Add Telecom"))
    _click(recorder, page, page.locator("select[data-testid=system]").nth(0))
    recorder.type_text("phone")
    recorder.press("Escape")
    _click(recorder, page, page.get_by_placeholder("Value").nth(0))
    recorder.type_text(values["phone_home"], param="phone_home")
    _click(recorder, page, page.get_by_role("button", name="Add Telecom"))
    _click(recorder, page, page.locator("select[data-testid=system]").nth(1))
    recorder.type_text("email")
    recorder.press("Escape")
    _click(recorder, page, page.get_by_placeholder("Value").nth(1))
    recorder.type_text(values["email"], param="email")

    _click(recorder, page, page.locator("input[name=gender]"))
    recorder.type_text(values["sex"], param="sex")
    page.wait_for_timeout(1200)
    recorder.press("ArrowDown")
    recorder.press("Enter")

    _click(recorder, page, page.locator("input[name=birthDate]"))
    recorder.type_text(values["DOB"], param="DOB")

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
