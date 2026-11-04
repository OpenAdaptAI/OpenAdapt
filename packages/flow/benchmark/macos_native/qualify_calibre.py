#!/usr/bin/env python3
"""Record, compile and run a write workflow on calibre (macOS, AX), governed.

calibre is a third-party, open-source (GPL-3) e-book manager. Its main window
is a Qt application; its library is a SQLite database (``metadata.db``) that
it writes as soon as an edit is committed. calibre is a test target only:
nothing of it is copied or shipped.

The workflow sets one book's tag: click the book's title in the library list,
click its Tags cell (a click on a cell of the selected row opens calibre's
in-place editor with the existing tag selected), type the new tag (a runtime
parameter), and click the title again, which commits the edit. calibre then
replaces the book's ``books_tags_link`` row. Every click lands on an AX
element with a name (the title, or the book's existing tag), and no special
key is pressed, so no step needs an operator risk review.

The script, on ``MacOSBackend`` (AX tree, screen capture, input):

1. records the workflow once on a fresh library and compiles it; the commit
   step gets a SQL record check (``SqlRecordVerifier`` on ``metadata.db``:
   the one book with this title and author has exactly the new tags) and the
   book's row (title and author) as its identity band;
2. runs the bundle governed (``standard`` profile, run gate, durable, settled,
   frame lease) on fresh libraries:

   ``clean``        the same book: must be ``VERIFIED``, with only that book's
                    tags changed;
   ``wrong_author`` planted fault: the book with that title is by another
                    author: must stop before the commit and write nothing;
   ``look_alike``   two books with the same title, author and tag: must refuse
                    the ambiguous target and write nothing.

A fault case counts only when the run gate admitted the bundle, so the stop
comes from the runtime's checks, not from a refused bundle.

Every calibre runs with its own library and config folders, never the
user's. Usage::

    python benchmark/macos_native/qualify_calibre.py

``--steady-caret`` holds the editor's text caret steady for the run: it sets
AppKit's ``NSTextInsertionPointBlinkPeriod`` in calibre's own defaults domain
(Qt follows it) and restores the previous values afterwards. With a blinking
caret, the commit click is refused at the frame check (#1713).

OCR (the identity bands and visual anchors) uses the operating system's own
engine, ``OPENADAPT_FLOW_OCR=native`` (Apple Vision, the ``native-ocr``
extra), for the demonstration and every run: RapidOCR takes over ten seconds
on calibre's full window, past the backend's ten-second frame lease.

Writes ``benchmark/macos_native/results/calibre-<date>[-steady-caret].json``. Needs macOS
with Screen Recording and Accessibility access and calibre in
/Applications. It takes the screen for a few minutes.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

FLOW = Path(__file__).resolve().parents[2]
if str(FLOW) not in sys.path:
    sys.path.insert(0, str(FLOW))

CALIBRE = Path("/Applications/calibre.app/Contents/MacOS")
RESULTS = Path(__file__).resolve().parent / "results"
LIBRARY = "clinic"
TITLE = f"calibre — || {LIBRARY} ||"
BOOK = ("Diabetes Care Guide", "Ana Ruiz")
#: The tag every book starts with: the Tags cell's AX name.
BOOK_TAG = "endocrine"
OTHER_BOOKS = [
    ("Asthma Action Plan", "Ben Okafor", "respiratory"),
    ("Stroke Rehab Basics", "Li Wei", "neurology"),
]
DEMO_TAG = "follow-up"
RUN_TAG = "review-q4"
#: How long the backend may wait for two identical frames after it moves the
#: pointer (default 2 s). One capture of calibre's full window takes up to a
#: few seconds on a loaded host, so two frames don't fit in two seconds. The
#: identical-frames requirement itself is unchanged.
POINTER_SETTLE_TIMEOUT_S = 6.0
#: Where the library window goes (screen points): clear of the top menu bar,
#: the Dock's reveal area and the screen center, where system prompts appear.
WINDOW_ORIGIN = (60, 450)
CASES: dict[str, list[tuple[str, str, str]]] = {
    "clean": [(*BOOK, BOOK_TAG), *OTHER_BOOKS],
    "wrong_author": [(BOOK[0], "Ana Ruiz-Ortega", BOOK_TAG), *OTHER_BOOKS],
    "look_alike": [(*BOOK, BOOK_TAG), (*BOOK, BOOK_TAG), *OTHER_BOOKS],
}
#: One record per book: its title, authors and tags, as calibre stores them.
BOOKS_SQL = (
    "SELECT b.id AS id, b.title AS title, "
    "(SELECT group_concat(a.name, ' & ') FROM books_authors_link ba "
    " JOIN authors a ON a.id = ba.author WHERE ba.book = b.id) AS author, "
    "(SELECT group_concat(t.name, ', ') FROM books_tags_link l "
    " JOIN tags t ON t.id = l.tag WHERE l.book = b.id) AS tags "
    "FROM books b"
)
RECORD_SQL = BOOKS_SQL + " WHERE b.title = :title"


def _library(root: Path, books: list[tuple[str, str, str]]) -> Path:
    """A fresh calibre library with ``books`` (no files, metadata only)."""
    folder = root / LIBRARY
    folder.mkdir(parents=True)
    for title, author, tag in books:
        subprocess.run(
            [
                str(CALIBRE / "calibredb"),
                "--library-path",
                str(folder),
                "add",
                "--empty",
                "--title",
                title,
                "--authors",
                author,
                "--tags",
                tag,
            ],
            check=True,
            capture_output=True,
            timeout=120,
            env=_calibre_env(root),
        )
    return folder


def _calibre_env(root: Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OPENADAPT_", "OAFLOW_"))
    }
    env["CALIBRE_CONFIG_DIRECTORY"] = str(root / "config")
    # calibre's own update check would open a notice window over the library.
    env["NO_CALIBRE_UPDATE"] = "1"
    return env


def _books(library: Path) -> list[tuple[str, str, str]]:
    """Every book's (title, author, tags), read from ``metadata.db``."""
    with sqlite3.connect(library / "metadata.db") as conn:
        return sorted((r[1], r[2] or "", r[3] or "") for r in conn.execute(BOOKS_SQL))


def _expected(case: str, tag: Optional[str]) -> list[tuple[str, str, str]]:
    """The library a case starts with; ``tag`` replaces the target's tag."""
    books = [
        (title, author, tag if tag and (title, author) == BOOK else old)
        for title, author, old in CASES[case]
    ]
    return sorted(books)


class Calibre:
    """One calibre process on its own library and config; killed by PID."""

    def __init__(self, root: Path, books: list[tuple[str, str, str]]):
        self.root = root
        self.library = _library(root, books)
        env = _calibre_env(root)
        # Skip the first-run wizard, as a person who set calibre up would have.
        subprocess.run(
            [
                str(CALIBRE / "calibre-debug"),
                "-c",
                "from calibre.utils.config import dynamic; "
                "dynamic.set('welcome_wizard_was_run', True)",
            ],
            check=True,
            capture_output=True,
            timeout=120,
            env=env,
        )
        self.proc = subprocess.Popen(
            [str(CALIBRE / "calibre"), "--with-library", str(self.library)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=20)
        time.sleep(1.5)


def _place_window(pid: int) -> None:
    """Move the library window to :data:`WINDOW_ORIGIN`."""
    from ApplicationServices import (
        AXUIElementCopyAttributeValue,
        AXUIElementCreateApplication,
        AXUIElementSetAttributeValue,
        AXValueCreate,
        kAXValueCGPointType,
    )
    from Quartz import CGPoint

    app = AXUIElementCreateApplication(pid)
    _e, windows = AXUIElementCopyAttributeValue(app, "AXWindows", None)
    for window in windows or []:
        _e, title = AXUIElementCopyAttributeValue(window, "AXTitle", None)
        if title == TITLE:
            AXUIElementSetAttributeValue(
                window,
                "AXPosition",
                AXValueCreate(kAXValueCGPointType, CGPoint(*WINDOW_ORIGIN)),
            )


def _backend(app: Calibre) -> Any:
    from openadapt_flow.backends.macos_backend import MacOSBackend, QuartzMacAXClient
    from openadapt_flow.backends.remote_display import MacWindowClient

    client = MacWindowClient()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if len(client.find_windows("calibre", TITLE)) == 1:
            break
        time.sleep(0.5)
    _place_window(app.proc.pid)
    time.sleep(1.0)
    backend = MacOSBackend(
        client,
        app="calibre",
        window_title=TITLE,
        settle_s=0.05,
        foreground_settle_s=0.1,
        ax_client=QuartzMacAXClient(),
        pointer_settle_timeout_s=POINTER_SETTLE_TIMEOUT_S,
    )
    backend.ensure_foreground()
    # While a newly launched app's Dock icon bounces, an auto-hidden Dock
    # lays a full-screen window over every app, and the backend refuses input
    # under it. Wait until the library window is topmost at its center.
    window = client.find_windows("calibre", TITLE)[0]
    x, y, w, h = window.bounds
    clear_since: Optional[float] = None
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if client.window_id_at_point(x + w / 2, y + h / 2) != window.window_id:
            clear_since = None
        elif clear_since is None:
            clear_since = time.monotonic()
        elif time.monotonic() - clear_since >= 2.0:
            break
        time.sleep(0.25)
    time.sleep(0.5)
    # A window that just appeared can't always be captured yet.
    for attempt in range(10):
        try:
            backend.screenshot()
            break
        except Exception:
            if attempt == 9:
                raise
            time.sleep(1.0)
    return backend


def _region(backend: Any, role: str, name: str) -> tuple[int, int, int, int]:
    from openadapt_flow.ir import StructuralLocator

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        handle = backend.locate_structural(
            StructuralLocator(role=role, name=name, window_name=TITLE)
        )
        if handle is not None and handle.region is not None:
            return handle.region
        time.sleep(0.3)
    raise RuntimeError(f"no {role} {name!r} in the calibre window")


CALIBRE_DOMAIN = "net.kovidgoyal.calibre"
BLINK_KEYS = {
    "NSTextInsertionPointBlinkPeriodOn": "100000000",
    "NSTextInsertionPointBlinkPeriodOff": "0",
}


class SteadyCaret:
    """Set calibre's caret blink defaults for the run; restore them after."""

    def __enter__(self) -> "SteadyCaret":
        self.saved: dict[str, Optional[str]] = {}
        for key, value in BLINK_KEYS.items():
            read = subprocess.run(
                ["defaults", "read", CALIBRE_DOMAIN, key],
                capture_output=True,
                text=True,
            )
            self.saved[key] = read.stdout.strip() if read.returncode == 0 else None
            subprocess.run(
                ["defaults", "write", CALIBRE_DOMAIN, key, "-float", value],
                check=True,
            )
        return self

    def __exit__(self, *_exc: object) -> None:
        for key, value in self.saved.items():
            if value is None:
                subprocess.run(["defaults", "delete", CALIBRE_DOMAIN, key], check=False)
            else:
                subprocess.run(
                    ["defaults", "write", CALIBRE_DOMAIN, key, "-float", value],
                    check=False,
                )


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=FLOW
    ).stdout.strip()


def _record(root: Path) -> tuple[Path, dict[str, Any]]:
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import Workflow
    from openadapt_flow.policy import evaluate_policy, load_policy
    from openadapt_flow.recorder import Recorder
    from openadapt_flow.runtime.effects import Effect, EffectKind
    from openadapt_flow.runtime.effects.effect import ValueExpr

    app = Calibre(root / "app", CASES["clean"])
    try:
        backend = _backend(app)
        recording = root / "recording"
        recorder = Recorder(
            backend, recording, settle_interval_s=0.05, settle_timeout_s=3.0
        )
        tx, ty, tw, th = _region(backend, "text", BOOK[0])
        ax, ay, aw, ah = _region(backend, "text", BOOK[1])
        tag = _region(backend, "text", BOOK_TAG)
        row = (tx, min(ty, ay), ax + aw - tx, max(th, ah))
        title = (tx + tw // 2, ty + th // 2)
        gx, _gy, gw, _gh = tag
        # The editor opens with the existing tag selected; typing replaces it.
        tag_cell = (gx + gw // 2, ty + th // 2)
        recorder.click(*title)
        recorder.click(*tag_cell)
        recorder.type_text(DEMO_TAG, param="tag")
        recorder.click(*title)
        recorder.finish()
        time.sleep(1.0)
        demo_rows = [
            b for b in _books(app.library) if b[0] in {x[0] for x in CASES["clean"]}
        ]
    finally:
        app.close()
    # The row's title and author are every click's identity band (the
    # record a person checks before tagging).
    events_path = recording / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    pad = 4
    band = [row[0] - pad, row[1] - pad, row[2] + 2 * pad, row[3] + 2 * pad]
    for event in events:
        if event.get("kind") == "click":
            event["identifier_region"] = band
    events_path.write_text("".join(json.dumps(e) + "\n" for e in events))
    bundle = root / "bundle"
    workflow = compile_recording(recording, bundle, name="calibre-tag-book")
    commit = workflow.steps[-1]
    commit.effects = [
        # The book (the one record with this title and author) carries the
        # new tag. A wrong or second book matching fails the check.
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match={"title": BOOK[0], "author": BOOK[1]},
            field="tags",
            value=ValueExpr(param="tag"),
            # Setting a field twice lands once: the field is the key.
            idempotency_key=ValueExpr(param="tag"),
            key_field="tags",
            risk="irreversible",
            timeout_s=10.0,
        )
    ]
    commit.risk = "irreversible"
    # The operator's risk review of the steps that don't commit.
    for step in workflow.steps[:-1]:
        step.risk = "reversible"
    (bundle / "manifest.json").unlink(missing_ok=True)
    workflow.save(bundle, encrypt=bool(workflow.encrypted))
    workflow = Workflow.load(bundle)
    report = evaluate_policy(workflow, load_policy("clinical-write"))
    return bundle, {
        "demonstration_rows": demo_rows,
        "steps": [
            {
                "id": s.id,
                "action": s.action.value,
                "intent": s.intent,
                "structural": s.anchor.structural.model_dump(exclude_none=True)
                if s.anchor is not None and s.anchor.structural is not None
                else None,
                "identity_armed": bool(s.anchor and s.anchor.identifier_crop),
            }
            for s in workflow.steps
        ],
        "certified_clinical_write": report.passed,
        "certification": report.render() if not report.passed else "passed",
    }


def _run(root: Path, bundle: Path, case: str) -> dict[str, Any]:
    from openadapt_flow.ir import Workflow
    from openadapt_flow.repair.self_repair import AppSession, ReplayTarget
    from openadapt_flow.runtime.effects import SqlRecordVerifier

    holder: dict[str, Calibre] = {}

    def open_session(_fault: Optional[str]) -> Any:
        app = Calibre(root / f"app-{case}", CASES[case])
        holder["app"] = app
        try:
            backend = _backend(app)
        except BaseException:
            app.close()
            raise
        return AppSession(backend=backend, close=app.close)

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(holder["app"].library / "metadata.db")

    target = ReplayTarget(
        open_session,
        effect_verifier=SqlRecordVerifier(
            connect,
            RECORD_SQL,
            query_params={"title": BOOK[0]},
            poll_interval_s=0.2,
        ),
        policy="clinical-write",
        profile="standard",
        params={"tag": RUN_TAG},
        execution_target_kind="macos",
    )
    session = target.open(None)
    started = time.monotonic()
    try:
        report = target.execute(
            Workflow.load(bundle), bundle, session, root / f"run-{case}"
        )
        result: dict[str, Any] = {
            "status": report.execution_outcome,
            "success": report.success,
            "steps": [
                {
                    "step": r.step_id,
                    "ok": r.ok,
                    "actuation": r.actuation,
                    "identity": r.identity.model_dump(mode="json", exclude_none=True)
                    if r.identity is not None
                    else None,
                    "effect_verified": r.effect_verified,
                    "failure_category": r.failure_category,
                    "error": (r.error or "")[:500],
                }
                for r in report.results
            ],
        }
    except Exception as exc:  # noqa: BLE001 - a refusal is a result here
        result = {"status": "refused", "reason": str(exc)[:2000]}
    finally:
        time.sleep(1.0)
        rows = [
            b
            for b in _books(holder["app"].library)
            if b[0] in {x[0] for x in CASES[case]}
        ]
        session.close()
    result["duration_s"] = round(time.monotonic() - started, 1)
    result["rows"] = rows
    return result


def qualify(root: Path, *, steady_caret: bool) -> dict[str, Any]:
    bundle, compiled = _record(root / "record")
    runs = {case: _run(root, bundle, case) for case in CASES}

    def admitted(case: str) -> bool:
        return runs[case]["status"] not in ("refused", None)

    checks = {
        "demonstration_set_the_tag": compiled["demonstration_rows"]
        == _expected("clean", DEMO_TAG),
        "bundle_certified": compiled["certified_clinical_write"],
        "commit_identity_armed": compiled["steps"][-1]["identity_armed"],
        "every_click_has_an_ax_locator": all(
            s["structural"] for s in compiled["steps"] if s["action"] == "click"
        ),
        "clean_run_verified": runs["clean"]["status"] == "VERIFIED",
        "clean_run_changed_only_the_target": runs["clean"]["rows"]
        == _expected("clean", RUN_TAG),
        "wrong_author_admitted_then_stopped": admitted("wrong_author")
        and runs["wrong_author"]["status"] != "VERIFIED",
        "wrong_author_wrote_nothing": runs["wrong_author"]["rows"]
        == _expected("wrong_author", None),
        "look_alike_admitted_then_stopped": admitted("look_alike")
        and runs["look_alike"]["status"] != "VERIFIED",
        "look_alike_wrote_nothing": runs["look_alike"]["rows"]
        == _expected("look_alike", None),
    }
    version = subprocess.run(
        [str(CALIBRE / "calibre"), "--version"], capture_output=True, text=True
    ).stdout.strip()
    return {
        "schema": "openadapt.macos-real-app-qualification/v1",
        "app": {
            "name": "calibre",
            "version": version,
            "license": "GPL-3.0 (test target only; nothing copied or shipped)",
            "database": "SQLite metadata.db",
        },
        "backend": "MacOSBackend (AX)",
        "steady_caret": steady_caret,
        "pointer_settle_timeout_s": POINTER_SETTLE_TIMEOUT_S,
        "ocr": os.environ.get("OPENADAPT_FLOW_OCR", "default"),
        "workflow": "set one book's tag: title, Tags cell, type the tag, title",
        "at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git("rev-parse", "HEAD"),
        "git_dirty": [
            line
            for line in _git("status", "--porcelain", "--", ".").splitlines()
            if "macos_native/results/" not in line
        ],
        "compiled": compiled,
        "runs": runs,
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> int:
    os.environ["OPENADAPT_FLOW_OCR"] = "native"
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep", action="store_true", help="keep the work folder")
    parser.add_argument(
        "--steady-caret",
        action="store_true",
        help="hold calibre's text caret steady (restored afterwards)",
    )
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="oa-calibre-"))
    try:
        if args.steady_caret:
            with SteadyCaret():
                report = qualify(root, steady_caret=True)
        else:
            report = qualify(root, steady_caret=False)
    finally:
        if args.keep:
            print(f"work folder: {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)
    suffix = "-steady-caret" if args.steady_caret else ""
    out = args.output or RESULTS / (
        f"calibre-{datetime.now(timezone.utc):%Y%m%d}{suffix}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"calibre: {'PASSED' if report['passed'] else 'FAILED'} -> {out}")
    for name, ok in report["checks"].items():
        print(f"  {'ok ' if ok else 'BAD'} {name}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
