"""``openadapt-flow demo``: two real runs and one page that shows the difference.

The bundled tutorial already proves the product's claim in text. ``--break-it``
reruns the same checked automation against an app that paints "Encounter saved"
and then drops the write; the engine reads the record, finds nothing, and
stops. The two final screenshots are byte-identical. Only the record check
tells the runs apart.

This module runs that pair and writes one self-contained HTML page that shows
it: the two final screens side by side, the record check under each, the plain
result of each run, the choices a person gets when a run stops, what this means
for a real process, one next command, and the exact engine terms folded under
"Technical details".

Every statement on the page comes from the two runs' own evidence:

* each plain result comes from the run's ``transaction_outcome`` through
  :func:`openadapt_flow.plain_outcome.plain_result`, never from the coarse
  ``HALTED`` label, so the page never says "nothing was written" for a run the
  engine left at ``RECONCILIATION_REQUIRED``;
* record counts come from the demo app's own store, read out of band;
* the screenshots are the retained ``after`` frames of each run's last step,
  and "identical" is printed only when their SHA-256 digests match;
* the person's choices come from the halted run's ``pending_escalation.json``.

Nothing here changes how a run is admitted, executed, or verified.
"""

from __future__ import annotations

import base64
import hashlib
import html
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from openadapt_flow.cli_hints import command
from openadapt_flow.plain_outcome import PlainResult, plain_result

#: Default output directory, suffixed ``-2``, ``-3`` ... when it already exists.
DEFAULT_DEMO_DIR = "openadapt-demo"

#: The proof page's file name inside the output directory.
PAGE_NAME = "index.html"

#: The placeholder app address in the one next command.
NEXT_URL = "https://your-test-app.example"

#: The part of each final frame the cards show, in frame pixels from the top
#: left. On the bundled demo app this holds the patient banner, the saved
#: message, and the encounters list; the rest of the 1280x800 frame is empty
#: page. The full frame stays one click away and is what the identical-screens
#: check hashes.
CROP_WIDTH = 640
CROP_HEIGHT = 320


class DemoError(RuntimeError):
    """The demo could not run or could not produce honest evidence."""


# ---------------------------------------------------------------------------
# Output directory
# ---------------------------------------------------------------------------


#: The two run folders the tutorial writes inside the demo folder. The engine
#: reserves each one as a durable run path.
RUN_SUBDIRS = ("run", "run-broken")


def run_paths_reserved(root: Path) -> bool:
    """True when an earlier run already reserved one of the demo's run paths.

    The engine reserves every durable run path for good, even after its
    folder is deleted (``runtime/durable/authority.py``), and refuses a run
    that reuses one. This reads that registry so the demo can pick a free name
    up front. Read-only; when the registry can't be read, the answer is
    ``False`` and the engine's own refusal still applies.
    """

    import sqlite3
    from contextlib import closing

    from openadapt_flow.runtime.durable.authority import (
        _default_db_path,
        _path_key,
    )

    db_path = _default_db_path()
    if not db_path.is_file():
        return False
    keys = [_path_key(root / name) for name in RUN_SUBDIRS]
    try:
        with closing(
            sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        ) as connection:
            row = connection.execute(
                "SELECT 1 FROM durable_authority WHERE path_key IN (?, ?) LIMIT 1",
                keys,
            ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def choose_output_dir(out: Optional[str | Path], *, cwd: Optional[Path] = None) -> Path:
    """Pick the output directory. Never reuse a directory that has content.

    With no ``out``, use ``openadapt-demo`` in ``cwd`` and append ``-2``,
    ``-3`` ... until the name is free: not on disk, and not reserved by an
    earlier run whose folder was deleted. An explicit ``out`` that already
    holds files, or that an earlier run reserved, is refused, so an earlier
    proof is never overwritten and the engine never sees a reused run path.
    """

    if out is not None:
        root = Path(out).expanduser().resolve()
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise DemoError(
                f"{root} already exists and isn't empty. Pass --out with a new "
                "folder so the earlier demo stays as it is."
            )
        if run_paths_reserved(root):
            raise DemoError(
                f"an earlier demo already ran in {root}. OpenAdapt never reuses "
                "a run folder, even after it's deleted. Pass --out with a new "
                "folder."
            )
        return root
    base = (cwd or Path.cwd()).resolve()
    root = base / DEFAULT_DEMO_DIR
    suffix = 2
    while root.exists() or run_paths_reserved(root):
        root = base / f"{DEFAULT_DEMO_DIR}-{suffix}"
        suffix += 1
    return root


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass
class RunEvidence:
    """What one of the two runs showed, read from its own artifacts."""

    title: str
    run_dir: Path
    execution_outcome: str
    transaction_outcome: Optional[str]
    plain: PlainResult
    #: Rows in the demo app's store after the run, read out of band.
    records: int
    #: True when the app painted its success message at the save step.
    screen_showed_success: Optional[bool]
    final_screenshot: Optional[Path]
    final_screenshot_sha256: Optional[str]
    report_md: Path
    model_calls: int = 0
    effects_required: int = 0
    effects_confirmed: int = 0
    effects_refuted: int = 0
    execution_profile: Optional[str] = None
    total_ms: Optional[float] = None
    receipt: Optional[Path] = None
    #: Raw on-screen text the engine read at the halt (OCR), if any.
    screen_text: Optional[str] = None
    #: The engine's own halt reason, verbatim.
    halt_reason: str = ""


@dataclass
class PersonChoice:
    """One option a person gets when a run stops."""

    plain: str
    engine_text: str


@dataclass
class DemoEvidence:
    """Both runs plus what the page needs to tell their story."""

    out_dir: Path
    clean: RunEvidence
    broken: RunEvidence
    bundle_dir: Path
    fault: str
    #: The quoted name of the consequential step (for example "Save Encounter").
    save_step_name: Optional[str]
    choices: list[PersonChoice] = field(default_factory=list)
    pending_path: Optional[Path] = None
    generated_at: str = ""
    flow_version: str = ""

    @property
    def screens_identical(self) -> bool:
        return (
            self.clean.final_screenshot_sha256 is not None
            and self.clean.final_screenshot_sha256
            == self.broken.final_screenshot_sha256
        )

    @property
    def showed_the_difference(self) -> bool:
        """True when the pair proved the claim: one checked save, one stop."""

        return (
            self.clean.plain.key == "done_and_checked"
            and self.broken.execution_outcome == "HALTED"
            and self.clean.records >= 1
            and self.broken.records == 0
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def png_size(path: Optional[Path]) -> Optional[tuple[int, int]]:
    """Width and height from a PNG's IHDR chunk, or ``None`` if unreadable."""

    if path is None:
        return None
    try:
        with path.open("rb") as handle:
            head = handle.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    width = int.from_bytes(head[16:20], "big")
    height = int.from_bytes(head[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def _load_report(run_dir: Path) -> Any:
    from openadapt_flow.ir import RunReport

    path = run_dir / "report.json"
    if not path.is_file():
        return None
    try:
        return RunReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _safe_child(run_dir: Path, relative: Optional[str]) -> Optional[Path]:
    """Resolve a run-dir-relative frame path without leaving the run dir."""

    if not relative:
        return None
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    candidate = run_dir / rel
    return candidate if candidate.is_file() and not candidate.is_symlink() else None


def _final_frame(run_dir: Path, report: Any) -> Optional[Path]:
    """The retained ``after`` frame of the run's last executed step."""

    if report is None:
        return None
    for result in reversed(list(report.results)):
        if getattr(result, "skipped", False):
            continue
        frame = _safe_child(run_dir, getattr(result, "after_png", None))
        if frame is not None:
            return frame
    return None


def _quoted(intent: Optional[str]) -> Optional[str]:
    match = re.search(r"'([^']+)'", intent or "")
    return match.group(1) if match else None


#: Plain wording for the engine's durable-pause options. Matched on the
#: engine's own text (``runtime/durable/controller.py``). An option this table
#: doesn't know is shown verbatim, so the page never invents a choice.
_PLAIN_CHOICES: tuple[tuple[str, str], ...] = (
    (
        "Inspect the system of record and correct it",
        "Check the record in the app and correct it by hand.",
    ),
    (
        "Investigate the system of record",
        "Check the record in the app and correct it by hand.",
    ),
    (
        "Approve and RESUME from the last verified checkpoint",
        "After checking the record, let the run continue from the stopped "
        "step. Steps already confirmed don't repeat.",
    ),
    ("Abort the run", "Cancel the run."),
)


def plain_choice(engine_text: str) -> str:
    """Plain wording for one engine option, or the engine text unchanged."""

    for prefix, plain in _PLAIN_CHOICES:
        if engine_text.startswith(prefix):
            return plain
    return engine_text


def _person_choices(run_dir: Path) -> tuple[list[PersonChoice], Optional[Path]]:
    from openadapt_flow import crypto
    from openadapt_flow.runtime.durable.checkpoint import CheckpointStore

    try:
        store = CheckpointStore(run_dir, key=crypto.resolve_key(None))
        pending = store.read_pending()
    except Exception:  # noqa: BLE001 - a missing key or file only hides the list
        return [], None
    if pending is None:
        return [], None
    path = run_dir / "pending_escalation.json"
    choices = [
        PersonChoice(plain=plain_choice(text), engine_text=text)
        for text in pending.proposed_options
    ]
    return choices, (path if path.is_file() else None)


def _flow_version() -> str:
    try:
        from importlib.metadata import version

        return version("openadapt-flow")
    except Exception:  # noqa: BLE001 - editable or vendored installs
        from openadapt_flow import __version__

        return __version__


def collect_evidence(result: Any, out_dir: Path) -> DemoEvidence:
    """Read both runs' evidence from a ``TutorialResult`` and its artifacts."""

    broken = result.break_it
    if broken is None:
        raise DemoError("the demo needs both runs, but the second run is missing")

    clean_report = _load_report(Path(result.run_dir))
    broken_report = _load_report(Path(broken.run_dir))

    clean_frame = _final_frame(Path(result.run_dir), clean_report)
    broken_frame = _final_frame(Path(broken.run_dir), broken_report)

    save_intent = None
    if broken_report is not None and broken_report.halt is not None:
        save_intent = broken_report.halt.intent
    clean_screen_ok: Optional[bool] = None
    if clean_report is not None and clean_report.results:
        last = [r for r in clean_report.results if not r.skipped]
        if last:
            clean_screen_ok = last[-1].postconditions_ok
            save_intent = save_intent or last[-1].intent

    receipt = result.receipt_paths.get("png") if result.receipt_paths else None

    clean = RunEvidence(
        title="Run 1",
        run_dir=Path(result.run_dir),
        execution_outcome=str(result.execution_outcome),
        transaction_outcome=result.transaction_outcome,
        plain=plain_result(result.transaction_outcome, result.execution_outcome),
        records=int(result.system_of_record_records),
        screen_showed_success=clean_screen_ok,
        final_screenshot=clean_frame,
        final_screenshot_sha256=_sha256(clean_frame) if clean_frame else None,
        report_md=Path(result.run_dir) / "REPORT.md",
        model_calls=int(result.model_calls),
        effects_required=int(result.effects_required),
        effects_confirmed=int(result.effects_confirmed),
        execution_profile=result.execution_profile,
        total_ms=getattr(clean_report, "total_ms", None),
        receipt=Path(receipt) if receipt else None,
    )
    broken_run = RunEvidence(
        title="Run 2",
        run_dir=Path(broken.run_dir),
        execution_outcome=str(broken.execution_outcome),
        transaction_outcome=broken.transaction_outcome,
        plain=plain_result(broken.transaction_outcome, broken.execution_outcome),
        records=int(broken.system_of_record_records),
        screen_showed_success=bool(broken.screen_claimed_success),
        final_screenshot=broken_frame,
        final_screenshot_sha256=_sha256(broken_frame) if broken_frame else None,
        report_md=Path(broken.report_path),
        model_calls=int(getattr(broken_report, "model_calls", 0) or 0),
        effects_required=int(broken.effects_required),
        effects_refuted=int(broken.effects_refuted),
        execution_profile=getattr(broken_report, "execution_profile", None),
        total_ms=getattr(broken_report, "total_ms", None),
        screen_text=broken.screen_claim_text,
        halt_reason=broken.halt_reason,
    )
    choices, pending_path = _person_choices(Path(broken.run_dir))
    return DemoEvidence(
        out_dir=out_dir,
        clean=clean,
        broken=broken_run,
        bundle_dir=Path(result.bundle_dir),
        fault=broken.fault,
        save_step_name=_quoted(save_intent),
        choices=choices,
        pending_path=pending_path,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        flow_version=_flow_version(),
    )


# ---------------------------------------------------------------------------
# Plain wording shared by the page and the terminal summary
# ---------------------------------------------------------------------------


def _notes(count: int) -> str:
    return f"{count} note" if count == 1 else f"{count} notes"


def _screen_words(showed: Optional[bool]) -> str:
    if showed is True:
        return "Success message"
    if showed is False:
        return "No success message"
    return "Not recorded"


#: Plain results that mean the run stopped short of reporting done.
_STOPPED_KEYS = frozenset({"check_the_record", "stopped_before_saving"})


def run_headline(run: RunEvidence) -> str:
    """The card title for one run.

    The "screen said saved, record says no" title needs all three facts: the
    transaction outcome is a stop (never done or unchecked), the app painted
    its success message, and the record check found nothing. Otherwise the
    title is the plain result's own label.
    """

    if (
        run.plain.key in _STOPPED_KEYS
        and run.execution_outcome == "HALTED"
        and run.screen_showed_success
        and run.records == 0
    ):
        return "Stopped: the screen said saved, the record says no"
    return run.plain.label


def run_explanation(run: RunEvidence) -> str:
    """One or two sentences under the card title, from the plain result."""

    plain = run.plain
    if plain.key == "done_and_checked":
        return (
            "It saved the note, then read the record back and found it. Only "
            "then did it report the run as done."
        )
    if run.records == 0 and plain.key == "check_the_record":
        # The record check found nothing, but the engine did not prove that
        # no part of the save landed. Say both, and never claim absence.
        return (
            "The record check found no note. OpenAdapt only says nothing was "
            "saved when it can prove it, so a person checks the record before "
            "anything is retried."
        )
    return plain.meaning


def what_openadapt_did(run: RunEvidence, *, asked_a_person: bool = False) -> str:
    """One short phrase for the card's "OpenAdapt" row."""

    plain = run.plain
    if plain.key == "done_and_checked":
        return "Reported done"
    if plain.key in _STOPPED_KEYS and run.execution_outcome == "HALTED":
        if asked_a_person:
            return "Stopped and asked a person. No retry."
        return "Stopped. No retry."
    return plain.label


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

#: Styles for the proof page. Color is a verdict: green appears only for a
#: run whose save was read back and matched, amber only for a run that stopped
#: for a person, red only for a run that didn't finish; everything else is ink
#: and hairlines. Every status also carries an icon and a word. The page loads
#: no fonts: the named families render where installed and fall back to
#: Georgia and the system sans.
_CSS = """
:root {
  color-scheme: light;
  --ink: #0B1220; --ink-2: #414E63; --ink-3: #5A697F;
  --paper: #FFFFFF; --ground: #F5F7FA; --sunken: #EDF1F6;
  --rule: #DEE4EC; --rule-strong: #C6D0DC;
  --done: #0B7A5A; --done-strong: #07553E; --done-tint: #E8F3EE;
  --halt: #9A5B0A; --halt-strong: #7A4706; --halt-tint: #FCF3E6;
  --failed: #B3261E; --failed-tint: #FBEAE8;
  --focus: #2563EB;
  --serif: "Source Serif 4", Georgia, "Times New Roman", serif;
  --sans: "Public Sans", "Segoe UI", system-ui, -apple-system, sans-serif;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --ink: #E8EDF3; --ink-2: #B4BECB; --ink-3: #8A97A8;
    --paper: #151B23; --ground: #0E1319; --sunken: #1C2430;
    --rule: #262F3B; --rule-strong: #354152;
    --done: #3AA97F; --done-strong: #86D6B4; --done-tint: #12302A;
    --halt: #D29A48; --halt-strong: #EEC283; --halt-tint: #33260F;
    --failed: #F2877E; --failed-tint: #3A1A18;
    --focus: #7CA8FF;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --ink: #E8EDF3; --ink-2: #B4BECB; --ink-3: #8A97A8;
  --paper: #151B23; --ground: #0E1319; --sunken: #1C2430;
  --rule: #262F3B; --rule-strong: #354152;
  --done: #3AA97F; --done-strong: #86D6B4; --done-tint: #12302A;
  --halt: #D29A48; --halt-strong: #EEC283; --halt-tint: #33260F;
  --failed: #F2877E; --failed-tint: #3A1A18;
  --focus: #7CA8FF;
}
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--ground); color: var(--ink);
  font: 400 16px/1.55 var(--sans); -webkit-font-smoothing: antialiased;
}
a { color: inherit; text-underline-offset: 2px; }
a:focus-visible, summary:focus-visible {
  outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 4px;
}
svg { flex: none; }
main { max-width: 1000px; margin: 0 auto; padding: 40px 24px 72px; }
@media (max-width: 760px) { main { padding: 28px 16px 56px; } }
.provenance { margin: 0 0 14px; color: var(--ink-2); font-size: 14px; }
.provenance strong { color: var(--ink); font-weight: 600; }
h1 {
  margin: 0 0 14px; font: 420 clamp(2rem, 1.3rem + 2.1vw, 2.95rem)/1.1 var(--serif);
  letter-spacing: -0.016em; text-wrap: balance; max-width: 26ch;
}
.lede {
  margin: 0 0 32px; max-width: 68ch; font-size: 19px; color: var(--ink-2);
  text-wrap: pretty;
}
.runs { display: grid; grid-template-columns: 1fr 1fr; gap: 0 20px; }
@media (max-width: 640px) { .runs { grid-template-columns: 1fr; } }
.run {
  background: var(--paper); border: 1px solid var(--rule); border-radius: 12px;
  overflow: hidden; display: grid; grid-row: span 3; grid-template-rows: subgrid;
  row-gap: 0; margin-bottom: 20px;
}
.run.tone-done { border: 1.5px solid var(--done); }
.run.tone-stopped, .run.tone-check { border: 2px solid var(--halt); }
.run.tone-failed { border: 1.5px solid var(--failed); }
.run header { padding: 18px 20px 16px; border-bottom: 1px solid var(--rule); }
.run .which { margin: 0 0 6px; font-size: 14px; color: var(--ink-3); }
.run h2 {
  margin: 0; font: 450 24px/1.2 var(--serif); letter-spacing: -0.01em;
  text-wrap: balance;
}
.run h2.verdict { display: flex; align-items: center; gap: 10px; }
.run h2.verdict svg { width: 20px; height: 20px; }
.tone-done h2.verdict { color: var(--done-strong); }
.tone-stopped h2.verdict, .tone-check h2.verdict { color: var(--halt-strong); }
.tone-failed h2.verdict { color: var(--failed); }
.tone-unchecked h2.verdict { color: var(--ink-2); }
.run header p.why { margin: 10px 0 0; color: var(--ink); text-wrap: pretty; }
.chip {
  display: inline-flex; align-items: center; gap: 6px; height: 26px;
  margin-top: 12px; padding: 0 10px 0 8px; border-radius: 999px;
  font: 600 13px/1 var(--sans); white-space: nowrap;
}
.chip.tone-done { background: var(--done-tint); color: var(--done-strong); }
.chip.tone-stopped, .chip.tone-check {
  background: var(--halt-tint); color: var(--halt-strong);
}
.chip.tone-failed { background: var(--failed-tint); color: var(--failed); }
.chip.tone-unchecked { background: var(--sunken); color: var(--ink); }
.shot { margin: 0; background: var(--ground); border-bottom: 1px solid var(--rule); }
.shot .frame { position: relative; overflow: hidden; background: #fff; }
.shot .frame img { position: absolute; top: 0; left: 0; max-width: none;
  height: auto; }
.shot figcaption { padding: 6px 20px; font-size: 13px; color: var(--ink-3); }
.facts { display: grid; grid-template-columns: auto 1fr; gap: 10px 16px;
  margin: 0; padding: 16px 20px 20px; align-items: baseline;
  align-content: start; }
.facts dt { color: var(--ink-3); font-size: 14px; }
.facts dd { margin: 0; font-weight: 600; }
.facts dd.count {
  font: 500 32px/1 var(--serif); letter-spacing: -0.015em;
  font-feature-settings: "tnum";
}
.facts dd.count small {
  font: 600 15px/1 var(--sans); letter-spacing: 0; color: var(--ink-2);
}
.callout {
  margin: 20px 0 0; padding: 14px 18px; max-width: none; background: var(--paper);
  border: 1px solid var(--rule); border-left: 3px solid var(--ink);
  border-radius: 8px;
}
section { margin-top: 48px; }
section > h2 {
  margin: 0 0 12px; font: 450 clamp(1.5rem, 1.2rem + 1vw, 1.875rem)/1.15 var(--serif);
  letter-spacing: -0.012em;
}
.person {
  background: var(--paper); border: 1px solid var(--rule); border-radius: 12px;
  padding: 18px 20px;
}
.person .chip { margin: 0 0 10px; }
.person p { margin: 0 0 8px; max-width: 68ch; }
.person ol { margin: 8px 0 0; padding-left: 22px; max-width: 68ch; }
.person li { margin: 6px 0; }
.means { margin: 0; padding-left: 22px; max-width: 68ch; }
.means li { margin: 0 0 10px; }
pre.cmd {
  margin: 0; padding: 14px 16px; overflow-x: auto; background: var(--sunken);
  color: var(--ink); border: 1px solid var(--rule); border-radius: 8px;
  font-size: 14px; user-select: all; -webkit-user-select: all;
}
code { font-family: var(--mono); }
.hint { margin: 10px 0 0; color: var(--ink-2); font-size: 14px; }
details {
  margin-top: 48px; background: var(--paper); border: 1px solid var(--rule);
  border-radius: 12px; padding: 14px 20px;
}
summary { cursor: pointer; font-weight: 600; }
details p { max-width: 68ch; }
details h3 { font-size: 15px; margin: 18px 0 6px; }
.kv { display: grid; grid-template-columns: minmax(140px, 220px) 1fr; gap: 4px 14px;
  margin: 0; font-size: 14px; }
.kv dt { color: var(--ink-3); }
.kv dd { margin: 0; overflow-wrap: anywhere; }
.kv dd code { font-size: 13px; }
@media (max-width: 640px) {
  .kv { grid-template-columns: 1fr; gap: 0; }
  .kv dt { margin-top: 10px; }
}
details ul { margin: 4px 0; padding-left: 20px; font-size: 14px; }
footer { margin-top: 32px; color: var(--ink-3); font-size: 13px; }
@media print {
  body { background: #fff; }
  .run, .person { break-inside: avoid; }
}
"""

#: Status icons on a 12-unit grid, drawn in ``currentColor`` and always shown
#: next to their word: a check for a checked save, pause bars for a stop
#: before saving, a triangle when a save may have gone through, a dashed
#: circle for a run nobody checked, an X for a run that didn't finish, and a
#: person for a decision waiting on someone.
_ICON_PATHS = {
    "done": (
        '<path d="M2.2 6.3l2.4 2.4 5.2-5.4" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
    ),
    "stopped": (
        '<rect x="2.5" y="2" width="2.3" height="8" rx=".6" fill="currentColor"/>'
        '<rect x="7.2" y="2" width="2.3" height="8" rx=".6" fill="currentColor"/>'
    ),
    "check": (
        '<path d="M6 1.6l4.9 8.6H1.1z" fill="none" stroke="currentColor" '
        'stroke-width="1.5" stroke-linejoin="round"/>'
        '<path d="M6 5v2.3" stroke="currentColor" stroke-width="1.5" '
        'stroke-linecap="round"/><circle cx="6" cy="8.8" r=".8" fill="currentColor"/>'
    ),
    "unchecked": (
        '<circle cx="6" cy="6" r="4.4" fill="none" stroke="currentColor" '
        'stroke-width="1.5" stroke-dasharray="2.2 1.7"/>'
    ),
    "failed": (
        '<circle cx="6" cy="6" r="4.8" fill="none" stroke="currentColor" '
        'stroke-width="1.5"/><path d="M4.2 4.2l3.6 3.6M7.8 4.2L4.2 7.8" '
        'stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>'
    ),
    "person": (
        '<circle cx="6" cy="3.7" r="2.2" fill="currentColor"/>'
        '<path d="M1.7 11c.4-2.5 2.1-3.9 4.3-3.9s3.9 1.4 4.3 3.9z" '
        'fill="currentColor"/>'
    ),
}


def _icon(kind: str, size: int = 14) -> str:
    """An inline status icon; decorative, because its word sits beside it."""

    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 12 12" '
        f'aria-hidden="true" focusable="false">{_ICON_PATHS[kind]}</svg>'
    )


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _data_uri(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{payload}"


def _rel(path: Optional[Path], root: Path) -> str:
    if path is None:
        return "not written"
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _relative_link(path: Optional[Path], root: Path) -> Optional[str]:
    """A forward-slash link from the page to ``path``, if it sits under ``root``."""

    if path is None:
        return None
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _frame_figure(run: RunEvidence, alt: str, root: Path) -> str:
    """The top of the run's final frame, cropped by CSS, plus a full-size link.

    The embedded bytes are the retained frame unchanged, so the page shows the
    same pixels the identical-screens check hashed.
    """

    image = _data_uri(run.final_screenshot)
    if image is None:
        return (
            '<figure class="shot"><figcaption>No final screen was retained.'
            "</figcaption></figure>"
        )
    width, height = png_size(run.final_screenshot) or (1280, 800)
    crop_w = min(CROP_WIDTH, width)
    crop_h = min(CROP_HEIGHT, height)
    scale = width / crop_w * 100
    link = ""
    relative = _relative_link(run.final_screenshot, root)
    if relative is not None:
        link = (
            f' <span class="full-link"><a href="{_e(relative)}">Open the full '
            "screen</a>.</span>"
        )
    return (
        '<figure class="shot">'
        f'<div class="frame" style="aspect-ratio: {crop_w} / {crop_h}">'
        f'<img src="{image}" alt="{_e(alt)}" width="{width}" height="{height}" '
        f'style="width: {scale:.4g}%"></div>'
        f"<figcaption>Top of the final screen of this run.{link}</figcaption>"
        "</figure>"
    )


def _run_card(
    run: RunEvidence, which: str, alt: str, root: Path, *, asked_a_person: bool
) -> str:
    headline = run_headline(run)
    tone = run.plain.tone
    if headline == run.plain.label:
        # The title is the result itself: give it the icon and the color.
        title = f'<h2 class="verdict">{_icon(tone)}{_e(headline)}</h2>'
        chip = ""
    else:
        # A longer title in ink, with the result as a chip beneath it.
        title = f"<h2>{_e(headline)}</h2>"
        chip = (
            f'<span class="chip tone-{_e(tone)}">{_icon(tone)}'
            f"{_e(run.plain.label)}</span>"
        )
    unit = "note" if run.records == 1 else "notes"
    screen = _e(_screen_words(run.screen_showed_success))
    figure = _frame_figure(run, alt, root)
    did = _e(what_openadapt_did(run, asked_a_person=asked_a_person))
    return f"""
    <article class="run tone-{_e(tone)}">
      <header>
        <p class="which">{_e(run.title)}: {_e(which)}</p>
        {title}
        {chip}
        <p class="why">{_e(run_explanation(run))}</p>
      </header>
      {figure}
      <dl class="facts">
        <dt>Screen showed</dt><dd>{screen}</dd>
        <dt>Record check</dt>
        <dd class="count">{run.records} <small>{unit} found</small></dd>
        <dt>OpenAdapt</dt><dd>{did}</dd>
      </dl>
    </article>"""


def _headline(evidence: DemoEvidence) -> tuple[str, str]:
    lede = (
        "OpenAdapt read the record after each run, so it reported the first "
        "run as done and stopped the second one instead of guessing."
    )
    both_screens_said_saved = bool(
        evidence.clean.screen_showed_success and evidence.broken.screen_showed_success
    )
    if (
        evidence.showed_the_difference
        and both_screens_said_saved
        and evidence.clean.records == 1
    ):
        # Say what the record check found, not that run 2 wrote nothing: the
        # engine may leave that run at "Check the record".
        return (
            "The screen said \u201csaved\u201d both times. The record check "
            "found only one note.",
            lede,
        )
    if evidence.showed_the_difference:
        return (
            "The record check found a note after only one of the two runs.",
            lede,
        )
    return (
        "This demo didn't go the way it should have.",
        "The two runs below didn't show the expected difference. Each result is "
        "still reported exactly as the run ended. Check the technical details.",
    )


def _choices_section(evidence: DemoEvidence) -> str:
    if not evidence.choices:
        return ""
    step = evidence.save_step_name
    # Say what the check found, never that nothing was written: the engine
    # may leave this run at "Check the record".
    lead = (
        f"The record check didn't find the note from “{_e(step)}”. "
        if step and evidence.broken.records == 0
        else "The run stopped. "
    )
    items = "".join(f"<li>{_e(choice.plain)}</li>" for choice in evidence.choices)
    return f"""
  <section>
    <h2>What a person sees when a run stops</h2>
    <div class="person">
      <span class="chip tone-check">{_icon("person")}Waiting for a person</span>
      <p><strong>{lead}</strong>OpenAdapt waits for a person to choose:</p>
      <ol>{items}</ol>
      <p class="hint">It doesn't guess, and it doesn't retry the save on its own.</p>
    </div>
  </section>"""


def _kv(rows: Sequence[tuple[str, object]]) -> str:
    return (
        '<dl class="kv">'
        + "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in rows)
        + "</dl>"
    )


def _technical_details(evidence: DemoEvidence) -> str:
    root = evidence.out_dir
    clean, broken = evidence.clean, evidence.broken

    def ms(value: Optional[float]) -> str:
        return f"{value / 1000:.1f} s" if value else "not recorded"

    run1 = _kv(
        [
            ("execution_outcome", clean.execution_outcome),
            ("transaction_outcome", clean.transaction_outcome or "not recorded"),
            ("execution profile", clean.execution_profile or "not recorded"),
            (
                "effects confirmed",
                f"{clean.effects_confirmed}/{clean.effects_required} by an "
                "independent read of the system of record",
            ),
            ("records in the store", clean.records),
            ("model calls", clean.model_calls),
            ("run time", ms(clean.total_ms)),
            ("final frame SHA-256", clean.final_screenshot_sha256 or "none"),
            ("report", _rel(clean.report_md, root)),
            ("local receipt", _rel(clean.receipt, root)),
        ]
    )
    run2 = _kv(
        [
            ("execution_outcome", broken.execution_outcome),
            ("transaction_outcome", broken.transaction_outcome or "not recorded"),
            (
                "injected fault",
                f"{evidence.fault!r}: the server rejects the write after the "
                "app shows success",
            ),
            (
                "effects refuted",
                f"{broken.effects_refuted}/{broken.effects_required} by an "
                "independent read of the system of record",
            ),
            ("records in the store", broken.records),
            ("on-screen text (OCR)", broken.screen_text or "none"),
            ("engine halt reason", broken.halt_reason or "none"),
            ("model calls", broken.model_calls),
            ("final frame SHA-256", broken.final_screenshot_sha256 or "none"),
            ("report", _rel(broken.report_md, root)),
            ("pending decision", _rel(evidence.pending_path, root)),
        ]
    )
    options = "".join(f"<li>{_e(c.engine_text)}</li>" for c in evidence.choices)
    options_block = (
        f"<h3>Options exactly as the engine wrote them</h3><ul>{options}</ul>"
        if options
        else ""
    )
    reconcile_note = ""
    if broken.transaction_outcome == "RECONCILIATION_REQUIRED":
        reconcile_note = (
            f"<p>The store held {broken.records} rows after the run, and the "
            "run still ended <code>RECONCILIATION_REQUIRED</code>, not "
            "<code>HALTED_BEFORE_EFFECT</code>. The save declared "
            f"{broken.effects_required} effects, and the record check refuted "
            f"{broken.effects_refuted} of them before the engine stopped. The "
            "engine claims &ldquo;nothing was written&rdquo; only when every "
            "declared effect is proven absent.</p>"
        )
    bundle_arg = shlex.quote(str(evidence.bundle_dir))
    graph_arg = shlex.quote(str(root / "graph.html"))
    commands = [
        ("Same pair, as text", command("tutorial --break-it")),
        ("Program map", command(f"visualize {bundle_arg} -o {graph_arg}")),
        (
            "Read a run in plain words",
            command(f"explain {shlex.quote(str(broken.run_dir))}"),
        ),
        ("Answer a stop: record the decision", command("approve RUN_DIR")),
        ("Answer a stop: continue the run", command("resume RUN_DIR")),
    ]
    provenance = _kv(
        [
            ("data", "Synthetic recording, fake clinic data (MockMed)"),
            ("generated", evidence.generated_at),
            ("openadapt-flow", evidence.flow_version),
            ("compiled program", _rel(evidence.bundle_dir, root)),
        ]
    )
    command_rows = (
        '<dl class="kv">'
        + "".join(
            f"<dt>{_e(label)}</dt><dd><code>{_e(text)}</code></dd>"
            for label, text in commands
        )
        + "</dl>"
    )
    return f"""
  <details>
    <summary>Technical details</summary>
    <p>Both runs used the same compiled program, the same policy, and the same
    record check. The record check reads the demo app's store over HTTP
    (<code>GET /api/db</code>), a path the app itself never calls, so the screen
    can't influence it. The demo app stops when the demo ends, so the stopped
    run can't be resumed here.</p>
    {reconcile_note}
    <h3>Run 1: honest app</h3>{run1}
    <h3>Run 2: app that drops the save</h3>{run2}
    {options_block}
    <h3>Commands</h3>
    {command_rows}
    <h3>Provenance</h3>
    {provenance}
  </details>"""


def render_demo_page(evidence: DemoEvidence) -> str:
    """The self-contained proof page (inline CSS and images, no scripts)."""

    title, lede = _headline(evidence)
    identical = (
        "The two final screens above are identical, pixel for pixel. A tool "
        "that trusts the screen would report both runs as done."
        if evidence.screens_identical
        else "The two final screens above look the same. A tool that trusts "
        "the screen would report both runs as done."
    )
    next_cmd = command(f"record --backend web --url {NEXT_URL} --out my-task")
    asked = bool(evidence.choices)
    clean_card = _run_card(
        evidence.clean,
        "honest app",
        "Final screen of run 1: the app shows its saved message",
        evidence.out_dir,
        asked_a_person=asked,
    )
    broken_card = _run_card(
        evidence.broken,
        "app that drops the save",
        "Final screen of run 2: the same saved message",
        evidence.out_dir,
        asked_a_person=asked,
    )
    choices = _choices_section(evidence)
    details = _technical_details(evidence)
    provenance = "Fake clinic data, run on this computer."
    if evidence.clean.model_calls == 0 and evidence.broken.model_calls == 0:
        provenance += " No AI calls."
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenAdapt demo proof</title>
<meta name="description" content="Two runs of the same automation on a fake clinic app. The screen said saved both times; the record check told them apart.">
<style>{_CSS}</style>
</head>
<body>
<main>
  <p class="provenance"><strong>Synthetic recording.</strong> {_e(provenance)}</p>
  <h1>{_e(title)}</h1>
  <p class="lede">{_e(lede)} Both runs used the same automation, built from one
  recorded example of filing a follow-up note in a fake clinic app. For the
  second run, the app was set to drop the note right after it showed its
  success message.</p>

  <div class="runs">
    {clean_card}
    {broken_card}
  </div>
  <p class="callout">{_e(identical)}</p>
  {choices}

  <section>
    <h2>What this means for a real process</h2>
    <ul class="means">
      <li>Someone on your team shows OpenAdapt a task once, in the app you
      already use, and it repeats the task the same way each time.</li>
      <li>After each save it reads the record back through a separate path
      you set up once, such as a report, an API, or a read-only login, so
      &ldquo;done&rdquo; means the record changed.</li>
      <li>When the screen and the record disagree, it stops and asks a person
      instead of guessing or retrying.</li>
    </ul>
  </section>

  <section>
    <h2>Try it on a test copy of your own app</h2>
    <pre class="cmd"><code>{_e(next_cmd)}</code></pre>
    <p class="hint">This opens a browser and records the task while you do it once.</p>
  </section>
  {details}
  <footer>Generated {_e(evidence.generated_at)} by openadapt-flow {_e(evidence.flow_version)}. All data on this page is fake.</footer>
</main>
</body>
</html>
"""


def write_demo_page(evidence: DemoEvidence) -> Path:
    path = evidence.out_dir / PAGE_NAME
    path.write_text(render_demo_page(evidence), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Terminal summary and browser
# ---------------------------------------------------------------------------


def _run_summary(run: RunEvidence) -> list[str]:
    """One or two terminal lines for one run, from its plain result."""

    headline = run_headline(run)
    if headline != run.plain.label:
        first = f"{run.title}  {headline}."
    else:
        screen = {
            True: "The screen said saved",
            False: "No success message on screen",
            None: "Screen not recorded",
        }[run.screen_showed_success]
        first = (
            f"{run.title}  {run.plain.label}. {screen}; the record check found "
            f"{_notes(run.records)}."
        )
    lines = [first]
    if run.plain.key != "done_and_checked":
        lines.append(" " * (len(run.title) + 2) + run.plain.next_step)
    return lines


def summary_lines(evidence: DemoEvidence, page: Path, opened: bool) -> list[str]:
    """A short terminal summary: both results, the page, and one next command.

    A blank line, at most two lines per run (the result, plus the next step
    when the run isn't done and checked), the page, and the next command: at
    most seven lines, so the demo prints at most nine with its two-line intro
    and eight in the usual case.
    """

    where = "opened in your browser" if opened else "open it in a browser"
    return [
        "",
        *_run_summary(evidence.clean),
        *_run_summary(evidence.broken),
        f"Proof page: {page} ({where})",
        "Next: " + command(f"record --backend web --url {NEXT_URL} --out my-task"),
    ]


def display_available(
    *,
    platform: Optional[str] = None,
    environ: Optional[dict[str, str]] = None,
    isatty: Optional[Callable[[], bool]] = None,
) -> bool:
    """True when opening a browser tab is likely to reach a person."""

    platform = platform or sys.platform
    env = os.environ if environ is None else environ
    tty = isatty() if isatty is not None else sys.stdout.isatty()
    if not tty:
        return False
    if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
        return True
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        return False
    return platform in ("darwin", "win32")


def open_page(page: Path) -> bool:
    import webbrowser

    try:
        return bool(webbrowser.open(page.resolve().as_uri()))
    except Exception:  # noqa: BLE001 - opening is a convenience only
        return False


__all__ = [
    "DEFAULT_DEMO_DIR",
    "DemoError",
    "DemoEvidence",
    "PersonChoice",
    "RunEvidence",
    "choose_output_dir",
    "collect_evidence",
    "display_available",
    "open_page",
    "plain_choice",
    "png_size",
    "render_demo_page",
    "run_paths_reserved",
    "run_explanation",
    "run_headline",
    "summary_lines",
    "write_demo_page",
]
