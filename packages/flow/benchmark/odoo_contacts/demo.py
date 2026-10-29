"""Teach Odoo's new-contact form: a scripted demonstration, then compile.

    PYTHONPATH=.:benchmark/odoo_contacts python benchmark/odoo_contacts/demo.py \
        record --out DIR [--values FILE]
    PYTHONPATH=.:benchmark/odoo_contacts python benchmark/odoo_contacts/demo.py \
        compile --recording DIR --bundle DIR

Signing in and opening a new contact are setup, not part of the
demonstration (``qualify_hooks.open_session``). The demonstration marks the
contact an Individual (the form starts on Company), fills it, and saves it
with the form's save button. Every action goes through the ``Recorder``, so
the frames and events are what a person's demonstration would leave; record
time uses Playwright locators only to find where to click.

Synthetic values only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

#: The demonstration's contact (synthetic). Replays bring their own values.
DEFAULT_VALUES = {
    "name": "Rowan Achterberg",
    "street": "41 Quarry Lane",
    "city": "Worcester",
    "zip": "01609",
    "phone": "508-555-0147",
    "email": "rowan.achterberg@example.invalid",
}

SETTLE = {
    "settle_timeout_s": 10.0,
    "settle_stable_frames": 3,
    "settle_interval_s": 0.3,
}


def _click(recorder: Any, page: Any, locator: Any) -> None:
    box = locator.bounding_box()
    if box is None:
        raise RuntimeError("target is not on the page")
    recorder.click(int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
    page.wait_for_timeout(300)


def demonstrate(recorder: Any, page: Any, values: dict[str, str]) -> None:
    """Fill a new person contact once and save it."""

    _click(recorder, page, page.get_by_label("Individual"))
    _click(recorder, page, page.locator("div[name=name] input"))
    recorder.type_text(values["name"], param="name")
    for placeholder, param in (("Street...", "street"), ("City", "city"), ("ZIP", "zip")):
        _click(recorder, page, page.get_by_placeholder(placeholder, exact=True))
        recorder.type_text(values[param], param=param)
    # Phone and Email have a label beside them, not a placeholder.
    for param in ("phone", "email"):
        _click(recorder, page, page.locator(f"#{param}_0"))
        recorder.type_text(values[param], param=param)
    _click(recorder, page, page.get_by_role("button", name="Save manually"))
    page.wait_for_url(lambda url: not url.rstrip("/").endswith("/new"), timeout=30_000)
    page.wait_for_timeout(1500)


def cmd_record(args: argparse.Namespace) -> int:
    from types import SimpleNamespace

    import qualify_hooks

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
    workflow = compile_recording(Path(args.recording), bundle, name="odoo-new-contact")
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
