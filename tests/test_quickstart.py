"""`openadapt quickstart`: three acts, plain and live output, honest numbers.

The story runs against a fake engine here, so these tests take milliseconds.
The fake returns whatever facts a test gives it, and the output must follow:
nothing the quickstart prints may be a constant standing in for a run. The
real run is covered by ``test_real_quickstart`` (opt in with
``OPENADAPT_REAL_QUICKSTART=1``) and the weekly quickstart-lifecycle job.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import struct
import subprocess
import sys
import threading
import time
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Optional

import pytest
from click.testing import CliRunner

from openadapt import cli
from openadapt.quickstart import next_steps, run_quickstart
from openadapt.quickstart.engine import (
    RUN_NOTES,
    BreakFacts,
    DemoStep,
    RunFacts,
    SealFacts,
    SealPages,
    StepTicker,
    demo_step_label,
)
from openadapt.quickstart.images import (
    ITERM2,
    KITTY,
    beside_image,
    detect_image_protocol,
    image_sequence,
)
from openadapt.quickstart.story import NextSteps, Outcome, run_story
from openadapt.quickstart.view import Marks, PlainView, RichView, make_view, more_lines

SNAPSHOT = Path(__file__).parent / "snapshots" / "quickstart_plain.snapshot"

SCREEN_TEXT = "Encounter saved — Synthetic follow-up in two weeks"

DEMO = [
    ("Click Open", 0.4),
    ("Click New Encounter", 0.7),
    ("Click Triage", 0.9),
    ("Click Note", 1.1),
    ('Type "Synthetic follow-up in two weeks"', 1.3),
    ("Click Save Encounter", 1.6),
]


def _png(path: Path, width: int = 64, height: int = 32) -> Path:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x80" * (width * 3) for _ in range(height))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )
    return path


def _run(root: Path, index: int, **overrides) -> RunFacts:
    facts = RunFacts(
        index=index,
        note=RUN_NOTES[index - 1],
        run_dir=root / f"run-{index}",
        execution_outcome="VERIFIED",
        transaction_outcome="VERIFIED",
        checked=True,
        records=1,
        model_calls=0,
        cost_usd=0.0,
        effects_required=2,
        effects_confirmed=2,
        seconds=2.5 + index / 10,
        plain_label="Done and checked",
    )
    return replace(facts, **overrides)


def _broken(root: Path, **overrides) -> BreakFacts:
    facts = BreakFacts(
        run_dir=root / "run-broken",
        fault="optimistic",
        execution_outcome="HALTED",
        transaction_outcome="RECONCILIATION_REQUIRED",
        screen_showed_success=True,
        screen_text=SCREEN_TEXT,
        screenshot=None,
        new_records=0,
        effects_required=2,
        effects_refuted=1,
        halt_reason="record_written refuted",
        model_calls=0,
        seconds=7.7,
        stop_attributed=True,
    )
    return replace(facts, **overrides)


class FakeEngine:
    """Stands in for the browser and the engine; returns the facts it's given."""

    def __init__(
        self,
        root: Path,
        *,
        visible: bool = False,
        runs: Optional[dict[int, dict]] = None,
        broken: Optional[dict] = None,
        program_steps: int = 6,
        seal: Optional[SealFacts] = SealFacts("seal-ed25519-0123456789abcdef", 5),
        page: bool = True,
        demo=DEMO,
        note_param: bool = True,
        parallel: int = 5,
        **_ignored,
    ) -> None:
        self._note_param = note_param
        self._parallel = parallel
        self.root = root
        self.visible = visible
        self._runs = runs or {}
        self._broken = broken or {}
        self._program_steps = program_steps
        self._seal = seal
        self._page = page
        self._demo = demo
        self.calls: list[str] = []

    def __enter__(self):
        self.calls.append("enter")
        return self

    def __exit__(self, *exc):
        self.calls.append("exit")

    def record(self, on_step):
        self.calls.append("record")
        steps = [DemoStep(label, at) for label, at in self._demo]
        for step in steps:
            on_step(step)
        return steps

    def compile(self):
        self.calls.append("compile")
        return self._program_steps

    @property
    def program_name(self):
        return "local-quickstart"

    def readback_fields(self):
        return [
            ("patient_id", "p1", None),
            ("type", "Triage", None),
            ("note", None, "note"),
        ]

    def note_param(self):
        return self._note_param

    def start_workers(self):
        self.calls.append("start workers")

    @property
    def parallel(self):
        return self._parallel

    def run_all(self, notes, on_finish):
        for index, _note in enumerate(notes, start=1):
            self.calls.append(f"run {index}")
            on_finish(_run(self.root, index, **self._runs.get(index, {})))

    def before_prompt(self):
        self.calls.append("before prompt")

    def break_it(self):
        self.calls.append("break")
        broken = dict(self._broken)
        self._screen = broken.pop("screen_text", SCREEN_TEXT)
        return _broken(self.root, **broken)

    def read_screen(self, broken):
        return self._screen

    def seal(self, runs):
        self.calls.append("seal")
        if self._seal is None:
            return None
        return SealFacts(self._seal.key_id, sum(1 for run in runs if run.checked))

    def seal_broken(self, broken):
        self.calls.append("seal broken")
        return True

    def seal_pages(self, runs, broken, command=None):
        self.calls.append("seal pages")
        self.seal_command = command(self.root / "run-1") if command else None
        first = next((run for run in runs if run.checked), None)
        return SealPages(
            verified=first.run_dir / "seal.html" if first else None,
            broken=broken.run_dir / "seal.html" if broken else None,
        )

    def write_page(self, first, broken):
        self.calls.append("page")
        return self.root / "index.html" if self._page else None


def _clock(step: float = 0.5):
    now = [0.0]

    def tick() -> float:
        now[0] += step
        return now[0]

    return tick


def _steps(outcome: Outcome) -> NextSteps:
    return next_steps(cli._hint, cli._shell_quote, True)(outcome)


def _plain(engine, **kwargs) -> tuple[str, Outcome]:
    stream = io.StringIO()
    outcome = run_story(
        engine,
        PlainView(stream),
        next_steps=_steps,
        clock=_clock(),
        **kwargs,
    )
    return stream.getvalue(), outcome


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "prefix", "/opt/openadapt-env")
    return tmp_path / "openadapt-quickstart"


# ---------------------------------------------------------------------------
# Plain output (not a terminal)
# ---------------------------------------------------------------------------


def test_plain_output_matches_the_snapshot(root):
    output, _ = _plain(FakeEngine(root))
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(output, encoding="utf-8")
    assert output == SNAPSHOT.read_text(encoding="utf-8")


def test_plain_output_runs_the_three_acts_in_order(root):
    engine = FakeEngine(root)
    output, _ = _plain(engine)
    # Signing runs on a thread during Act 3, after the five runs.
    assert [call for call in engine.calls if call != "seal"] == [
        "enter",
        "start workers",
        "record",
        "compile",
        *(f"run {index}" for index in range(1, 6)),
        "break",
        "page",
        "exit",
        "seal broken",
        "seal pages",
    ]
    assert engine.calls.index("seal") > engine.calls.index("run 5")
    steps = [output.index(f"\n[{n}/5] ") for n in (1, 2, 3, 4, 5)]
    assert steps == sorted(steps)
    assert "\x1b[" not in output


def test_plain_output_has_no_internal_vocabulary(root):
    output, _ = _plain(FakeEngine(root))
    for term in (
        "persistence boundary",
        "effect contract",
        "Seal Oracle",
        "metering",
        "billable",
        "success rail",
        "RECONCILIATION_REQUIRED",
        "HALTED",
        "VERIFIED",
        "system of record",
        "tier",
        "fixture",
    ):
        assert term not in output, term


def test_verbose_adds_the_engine_detail(root):
    output, _ = _plain(FakeEngine(root), verbose=True)
    assert "transaction RECONCILIATION_REQUIRED" in output
    assert "engine: record_written refuted" in output
    assert str(root / "run-1" / "REPORT.md") in output


def test_finale_has_one_next_step_and_the_rest_behind_more(root):
    output, _ = _plain(FakeEngine(root))
    finale = output[output.index("\nYour Seal: ") + 1 :].rstrip("\n").splitlines()
    assert len(finale) <= 16, finale
    at = finale.index("Next: Give your agent this task as a tool")
    assert finale[at + 1] == (
        "  claude mcp add openadapt -- openadapt agent serve --allow-run"
    )
    assert sum(1 for line in finale if line.startswith("Next: ")) == 1
    assert "flow record" not in output
    assert "More next steps: openadapt quickstart --more" in finale
    assert sum(1 for line in finale if line.startswith("Done in ")) == 1


def test_more_lists_every_other_next_step(root):
    steps = _steps(Outcome(root=root, seal=SealFacts("k", 1)))
    lines = more_lines(steps, root, root / "run-1" / "seal.html")
    assert lines[0] == (
        "Give your agent this task as a tool (Claude Code): claude mcp add "
        "openadapt -- openadapt agent serve --allow-run"
    )
    joined = "\n".join(lines)
    assert "Try it on your own app: openadapt flow record --backend web" in joined
    assert (
        "Check every Seal again: openadapt seal verify openadapt-quickstart" in joined
    )
    assert "Open your Seal (offline): openadapt-quickstart/run-1/seal.html" in joined
    assert "https://openadapt.ai/seals" in joined


def test_quickstart_more_prints_and_runs_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", "/opt/openadapt-env")
    monkeypatch.setattr(
        "openadapt.quickstart.run_quickstart",
        lambda *a, **k: pytest.fail("--more ran the quickstart"),
    )
    with CliRunner().isolated_filesystem(temp_dir=tmp_path):
        (Path("openadapt-quickstart") / "run-1").mkdir(parents=True)
        Path("openadapt-quickstart/run-1/seal.json").write_text("{}")
        Path("openadapt-quickstart/run-1/seal.html").write_text("")
        result = CliRunner().invoke(cli.main, ["quickstart", "--more"])
    assert result.exit_code == 0, result.output
    assert "claude mcp add openadapt --" in result.output
    assert "seal verify openadapt-quickstart" in result.output
    assert "openadapt-quickstart/run-1/seal.html" in result.output


def test_no_break_skips_act_three(root):
    engine = FakeEngine(root)
    output, outcome = _plain(engine, break_it=False)
    assert "Break it" not in output
    assert "break" not in engine.calls
    assert "page" not in engine.calls
    assert outcome.broken is None
    assert "[5/5]" not in output
    assert "Your Seal: " in output


# ---------------------------------------------------------------------------
# Every number comes from the runs
# ---------------------------------------------------------------------------


def test_numbers_follow_the_runs(root):
    engine = FakeEngine(
        root,
        program_steps=9,
        runs={
            2: {"model_calls": 3, "cost_usd": 0.04, "seconds": 11.25},
            4: {
                "checked": False,
                "execution_outcome": "HALTED",
                "transaction_outcome": "HALTED_BEFORE_EFFECT",
                "plain_label": "Stopped before saving",
            },
        },
        demo=[("Click Go", 0.25), ('Type "x"', 3.75)],
    )
    output, outcome = _plain(engine)

    assert "Turn it into a program · 9 steps, local-quickstart" in output
    assert "Click Go" in output and "3.8s" in output
    assert "11.2s" in output  # run 2's own time
    assert "■ Stopped before saving" in output
    assert "4 of 5 saved and checked · 3 AI calls · $0.04" in output
    assert "5 of 5" not in output
    assert "0 AI calls" not in output
    # Four checked runs and the stopped one.
    assert "key seal-ed25519-0123456789abcdef · 5 reports signed" in output
    assert "■ 4 of 5 saved and checked" in output
    assert "4 of 5 runs · 3 model calls · $0.04 · " in output
    # A run that didn't check out replaces the next step with its report.
    assert "Next: A run didn't check out. Read its report" in output
    assert "  openadapt-quickstart/run-4/REPORT.md" in output
    assert "claude mcp add" not in output
    assert outcome.checked == 4


def test_a_repeated_run_says_so(root):
    output, _ = _plain(FakeEngine(root, runs={3: {"repeated": True}}))
    assert "✓ checked in the database (on a second try)" in output
    assert output.count("second try") == 1


def test_the_aha_line_appears_only_when_the_run_showed_it(root):
    shown, _ = _plain(FakeEngine(root))
    assert (
        'The screen said "Encounter saved". The database had nothing. A bot that '
        "trusts the screen reports Done. OpenAdapt stopped." in shown
    )

    landed, _ = _plain(FakeEngine(root, broken={"new_records": 1}))
    assert "The database had nothing" not in landed
    assert "1 new record" in landed

    quiet, _ = _plain(FakeEngine(root, broken={"screen_showed_success": False}))
    assert "A screen-trusting bot" not in quiet
    assert "reports Done" not in quiet

    missed, _ = _plain(FakeEngine(root, broken={"execution_outcome": "VERIFIED"}))
    assert "Not marked done" not in missed
    assert "did not stop" in missed


def test_a_stop_the_record_check_did_not_cause_is_not_the_demonstration(root):
    # Codex review on #1431: a halt for another reason (here a readiness halt
    # before the save, nothing refuted, nothing read off the screen) must not
    # read as the record check catching the silent failure.
    output, _ = _plain(
        FakeEngine(
            root,
            broken={
                "screen_text": None,
                "screen_showed_success": False,
                "effects_refuted": 0,
                "stop_attributed": False,
                "transaction_outcome": "HALTED_BEFORE_EFFECT",
                "halt_reason": "readiness: the page never settled before step_000",
            },
        )
    )
    assert "It found nothing" not in output
    assert "reports Done" not in output
    assert "a saved message" not in output, "the screen was never observed"
    assert "Not marked done" not in output
    assert "not at the check this act sets off" in output


def test_an_engine_without_run_inputs_does_not_claim_new_notes(root):
    output, _ = _plain(FakeEngine(root, note_param=False))
    assert "new notes" not in output
    assert "[4/5] Run it 5 times · all at once" in output


@pytest.mark.parametrize(
    ("parallel", "subtitle"),
    [(5, "all at once"), (3, "3 at a time"), (1, "new notes\n")],
)
def test_act_two_says_how_many_run_at_once(root, parallel, subtitle):
    output, _ = _plain(FakeEngine(root, parallel=parallel))
    assert subtitle in output


def test_no_seal_means_no_seal_line_and_no_verify_command(root):
    engine = FakeEngine(root, seal=None)
    output, _ = _plain(engine)
    assert "Seal" not in output
    assert "seal verify" not in output
    assert "seal pages" not in engine.calls


def test_the_finale_names_the_persons_own_seal_then_the_real_ones(root):
    engine = FakeEngine(root)
    opened = []
    output, outcome = _plain(engine, open_page=lambda page: opened.append(page) or True)
    finale = output[output.index("\nYour Seal: ") + 1 :].splitlines()
    at = finale.index("  Open it: openadapt-quickstart/run-1/seal.html")
    assert finale[at + 1] == (
        "    opened in your browser; it checks the signature in your browser, "
        "offline; nothing is uploaded"
    )
    assert (
        finale[at + 2] == "  Real Seals from OpenEMR runs: https://openadapt.ai/seals"
    )
    # The person's own Seal opens; the side-by-side page does not.
    assert opened == [root / "run-1" / "seal.html"]
    assert outcome.seal_page_opened
    assert outcome.seal_pages.broken == root / "run-broken" / "seal.html"
    assert "side by side: openadapt-quickstart/index.html\n" in output
    # The page's own command checks that run folder.
    assert engine.seal_command == "openadapt seal verify openadapt-quickstart/run-1"


def test_a_seal_page_that_fails_leaves_the_seal_and_no_page_line(root):
    class NoPages(FakeEngine):
        def seal_pages(self, runs, broken, command=None):
            raise RuntimeError("no viewer")

    output, outcome = _plain(NoPages(root), open_page=lambda page: True)
    assert "Open it" not in output
    # The stopped run's Seal was written, so it counts without its page.
    assert "key seal-ed25519-0123456789abcdef · 6 reports signed" in output
    assert not outcome.seal_page_opened


def test_the_stopped_run_counts_only_when_its_seal_was_written(root):
    class NoBrokenSeal(FakeEngine):
        def seal_broken(self, broken):
            raise RuntimeError("key_unavailable")

    output, outcome = _plain(NoBrokenSeal(root), open_page=lambda page: True)
    assert "key seal-ed25519-0123456789abcdef · 5 reports signed" in output
    # The person's own Seal page is still written and opened.
    assert "  Open it: openadapt-quickstart/run-1/seal.html" in output
    assert outcome.seal_page_opened


def test_quickstart_exit_code_follows_the_runs(monkeypatch, tmp_path):
    def fake(root, **kwargs):
        outcome = Outcome(root=root)
        outcome.runs = [_run(root, 1), _run(root, 2, checked=False)]
        return outcome

    monkeypatch.setattr("openadapt.quickstart.run_quickstart", fake)
    monkeypatch.setattr("openadapt.cli._flow_runs_quickstart", lambda: True)
    with CliRunner().isolated_filesystem(temp_dir=tmp_path):
        result = CliRunner().invoke(cli.main, ["quickstart", "--out", "qs"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Interactive prompt
# ---------------------------------------------------------------------------


def test_no_prompt_without_a_terminal(root, monkeypatch):
    monkeypatch.setattr(
        "builtins.input", lambda *a: pytest.fail("prompted without a terminal")
    )
    monkeypatch.setattr("openadapt.quickstart.engine.FlowEngine", FakeEngine)
    monkeypatch.setattr("openadapt.quickstart.engine.display_available", lambda: False)
    stream = io.StringIO()
    run_quickstart(
        root, visible=False, break_it=True, verbose=False, steps=_steps, stream=stream
    )
    assert "Press Enter" not in stream.getvalue()
    assert "[5/5] Break the next save" in stream.getvalue()


def test_a_terminal_gets_the_prompt_and_waiting_is_not_counted(root, monkeypatch):
    asked = []

    def enter(*args):
        asked.append(True)
        time.sleep(0.01)
        return ""

    monkeypatch.setattr("builtins.input", enter)
    clock_values = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 105.0, 106.0])
    stream = io.StringIO()
    outcome = run_story(
        FakeEngine(root),
        PlainView(stream),
        next_steps=_steps,
        interactive=True,
        clock=lambda: next(clock_values),
    )
    assert asked == [True]
    assert "Press Enter to make the next save fail silently" in stream.getvalue()
    # 106 s elapsed, 100 s of it waiting at the prompt.
    assert outcome.total_seconds == pytest.approx(6.0)


@pytest.mark.parametrize(
    ("environ", "platform", "tty", "shown"),
    [
        ({}, "darwin", True, True),
        ({}, "linux", True, False),
        ({"DISPLAY": ":0"}, "linux", True, True),
        ({"SSH_CONNECTION": "x"}, "darwin", True, False),
        ({}, "darwin", False, False),
    ],
)
def test_display_available_needs_no_engine(environ, platform, tty, shown):
    from openadapt.quickstart.engine import display_available

    assert (
        display_available(platform=platform, environ=environ, isatty=lambda: tty)
        is shown
    )


@pytest.mark.parametrize(
    ("visible", "platform", "environ", "opened"),
    [
        (True, "darwin", {"__CFBundleIdentifier": "com.apple.Terminal"}, True),
        (False, "darwin", {"__CFBundleIdentifier": "com.apple.Terminal"}, False),
        (True, "linux", {"__CFBundleIdentifier": "com.apple.Terminal"}, False),
        (True, "darwin", {}, False),
    ],
)
def test_focus_returns_to_the_terminal_before_the_prompt(
    visible, platform, environ, opened
):
    from openadapt.quickstart.engine import return_focus_to_terminal

    calls = []
    assert (
        return_focus_to_terminal(
            visible=visible,
            platform=platform,
            environ=environ,
            run=lambda argv, **kw: calls.append(argv),
        )
        is opened
    )
    assert calls == ([["open", "-b", "com.apple.Terminal"]] if opened else [])


def test_end_of_input_at_the_prompt_continues(root, monkeypatch):
    def eof(*args):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    output = io.StringIO()
    outcome = run_story(
        FakeEngine(root), PlainView(output), next_steps=_steps, interactive=True
    )
    assert outcome.broken is not None


# ---------------------------------------------------------------------------
# Choosing a view
# ---------------------------------------------------------------------------


class _Terminal(io.StringIO):
    encoding = "utf-8"

    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(
    ("stream", "environ", "plain"),
    [
        (io.StringIO(), {}, True),
        (_Terminal(), {}, False),
        (_Terminal(), {"NO_COLOR": "1"}, True),
        (_Terminal(), {"NO_COLOR": ""}, False),
        (_Terminal(), {"TERM": "dumb"}, True),
    ],
)
def test_plain_lines_off_a_terminal_or_without_color(stream, environ, plain):
    view = make_view(stream, environ=environ)
    assert isinstance(view, PlainView) is plain


def test_terminals_without_unicode_get_ascii_marks(root):
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    view = PlainView(stream)
    assert (view.marks.ok, view.marks.stop) == ("OK", "STOP")
    assert Marks("utf-8").ok == "✓"


# ---------------------------------------------------------------------------
# Live terminal view
# ---------------------------------------------------------------------------


def _rich(root, environ, width: int = 110, **engine_kwargs) -> str:
    stream = _Terminal()
    view = RichView(stream, environ=environ, width=width, animate=False)
    run_story(
        FakeEngine(root, **engine_kwargs),
        view,
        next_steps=_steps,
        clock=_clock(),
    )
    return stream.getvalue()


def _visible(text: str) -> str:
    """Terminal output without color or hyperlink escapes."""

    import re

    text = re.sub(r"\x1b\]8;[^\x1b]*\x1b\\", "", text)
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_terminal_finale_is_short_with_every_optional_line(root, monkeypatch):
    stream = _Terminal()
    outcome = Outcome(root=root, seal=SealFacts("seal-ed25519-0", 5))
    outcome.runs = [_run(root, index) for index in range(1, 6)]
    steps = NextSteps(
        verify="openadapt seal verify qs",
        record="openadapt flow record",
        agent="openadapt agent serve --allow-run",
        keep="uv tool install --python 3.12 openadapt",
        shadow="Your shell's openadapt is 1.5.1 at /opt/x; the commands above use uvx.",
    )
    RichView(stream, environ={}, width=110, animate=False).finale(outcome, steps)
    lines = [line for line in _visible(stream.getvalue()).splitlines() if line.strip()]
    assert len(lines) <= 20, lines
    assert lines[-1].startswith("Your shell's openadapt is 1.5.1")
    assert "uv tool install" not in "\n".join(lines)


def test_terminal_view_tells_the_story_with_boxes_and_one_next_step(root):
    output = _rich(root, {"TERM_PROGRAM": "Apple_Terminal"})
    text = _visible(output)
    assert "\x1b[" in output
    assert "✓  Clinic app ready" in text
    assert "Open › New Encounter › Triage › Note" in text
    assert "─  Saved and checked  ─" in text
    assert '✓ note        "Follow-up in two weeks"' in text
    assert "╭─ The screen" in text and "╭─ The database" in text
    assert "Encounter saved — Synthetic" in text
    assert "✗ 0 new records" in text
    assert "A screen-trusting bot  ✓ Done" in text
    assert "■ Stopped. Not marked done." in text
    assert "a person checks the record before anything is retried" in text
    assert "◆ Your Seal" in text and "●─●─●─●─●─◆" in text
    assert "claude mcp add openadapt -- openadapt agent serve --allow-run" in text
    assert text.count("Next  ") == 1
    assert "flow record" not in text
    assert "\x1b]1337;" not in output and "\x1b_G" not in output


def test_terminal_view_links_the_seal_page(root):
    output = _rich(root, {})
    uri = (root / "run-1" / "seal.html").resolve().as_uri()
    assert "\x1b]8;id=" in output and uri in output
    assert "https://openadapt.ai/seals" in output


def test_narrow_terminal_stacks_and_never_boxes_a_wrapped_command(root):
    output = _visible(_rich(root, {}, width=60))
    lines = output.splitlines()
    # Only the command is left for the terminal to wrap, so a copy of it
    # carries no border characters.
    wide = [line for line in lines if len(line) > 60]
    assert wide == ["› claude mcp add openadapt -- openadapt agent serve --allow-run"]
    # The screen and the record stack: no line holds both boxes.
    assert not any("The screen" in line and "The database" in line for line in lines)
    assert "› claude mcp add openadapt -- openadapt agent serve --allow-run" in output


def test_a_terminal_animates_and_a_recording_does_not(root):
    assert RichView(_Terminal(), environ={}).animate is True
    assert RichView(io.StringIO(), environ={}).animate is False


def test_the_seal_card_reads_the_runs_receipt(root):
    run = root / "run-1"
    run.mkdir(parents=True)
    (run / "receipt.json").write_text(json.dumps(RECEIPT))
    (run / "seal.json").write_text(json.dumps(SEAL))
    plain, _ = _plain(FakeEngine(root))
    assert "Your Seal: Saved and checked" in plain
    assert "6 of 6 steps · 2 of 2 record checks · 0 model calls" in plain
    assert "ed25519 over report sha256 6dd4a7f2d1ea" in plain
    assert "VERIFIED" not in plain
    text = _visible(_rich(root, {}))
    assert "6 of 6 steps · 2 of 2 record checks · 0 model calls" in text


RECEIPT = {
    "schema_version": "openadapt.run-receipt/v2",
    "outcome": "VERIFIED",
    "transaction_outcome": "VERIFIED",
    "steps_ok": 6,
    "steps_total": 6,
    "effects_confirmed": 2,
    "effects_required": 2,
    "postconditions_confirmed": 9,
    "postconditions_required": 9,
    "model_calls": 0,
    "bundle_digest": "b025b630b5faedffc6f96cd630e140a61751ff2233c0d654b6ce7d55ec11e736",
    "receipt_digest": "1905fc77e1ea45c1bcdd62173cd7270ef987eb420f7cf81e3ee187fda54dbb24",
}
SEAL = {
    "schema": "openadapt.local-seal/v1",
    "algorithm": "ed25519",
    "issuer": "local",
    "key_id": "seal-ed25519-accd03a9246d29bb",
    "public_key": "JpfLXYLyY0CVqHz4tHY8eTyC9e7ggpoxLPq6ITgDeHU=",
    "outcome": "VERIFIED",
    "run_id": "run-1",
    "subject_kind": "run-report",
    "subject_sha256": "6dd4a7f2d1ea7954acbcf199cc83f9e7102f941eb8003df34043242186bf0d92",
    "issued_at": "2026-11-19T01:45:03Z",
    "signature": "8/w6JW9Juu4nKcCAowDP3y8trO9Ryf/Ucb4n5hHF1CwmuHeU2+stUmygDiylvgtRWurorcOVAdz6qEjvswr+CQ==",
}


@pytest.mark.parametrize(
    ("environ", "marker"),
    [
        ({"TERM_PROGRAM": "iTerm.app"}, "\x1b]1337;File=inline=1;"),
        ({"TERM_PROGRAM": "WezTerm"}, "\x1b]1337;File=inline=1;"),
        ({"KITTY_WINDOW_ID": "1", "TERM": "xterm-kitty"}, "\x1b_Ga=T,f=100"),
        ({"TERM_PROGRAM": "ghostty"}, "\x1b_Ga=T,f=100"),
    ],
)
def test_terminal_view_draws_the_screenshot_where_supported(root, environ, marker):
    root.mkdir()
    shot = _png(root / "shot.png", 1280, 800)
    output = _rich(root, environ, broken={"screenshot": shot})
    assert marker in output
    assert "The database" in output
    # The text boxes are the fallback, not drawn as well.
    assert "╭─ The screen" not in _visible(output)


def test_terminal_view_falls_back_when_the_screenshot_is_missing(root):
    output = _rich(root, {"TERM_PROGRAM": "iTerm.app"}, broken={"screenshot": None})
    assert "\x1b]1337;" not in output
    assert "╭─ The screen" in _visible(output)


def test_engine_plaintext_notices_collapse_to_one_line(root, monkeypatch):
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    stream = io.StringIO()
    view = PlainView(stream)

    class Noisy(FakeEngine):
        def compile(self):
            print(
                "WARNING: writing a compiled bundle in plaintext at /x/bundle: it "
                "can't be encrypted yet",
                file=sys.stderr,
            )
            print("something else", file=sys.stderr)
            return super().compile()

    with view.collect_notices():
        run_story(Noisy(root), view, next_steps=_steps, clock=_clock())
    assert err.getvalue() == "something else\n"
    assert (
        "The recording, program and run reports in openadapt-quickstart are "
        "unencrypted (synthetic data only)" in stream.getvalue()
    )


def test_the_new_key_notice_is_held_for_the_finale(root, monkeypatch, tmp_path):
    from openadapt_flow import at_rest

    monkeypatch.delenv("OPENADAPT_FLOW_PLAINTEXT_AT_REST", raising=False)
    monkeypatch.setenv("OPENADAPT_FLOW_AT_REST_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENADAPT_FLOW_KEY_STORE", "file")
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    stream = io.StringIO()
    view = PlainView(stream)
    with view.collect_notices():
        # Created before the story starts, so workers find it.
        assert at_rest.local_key_path().exists()
        run_story(FakeEngine(root), view, next_steps=_steps, clock=_clock())
    assert err.getvalue() == ""
    # The local passphrase and the device key new runs are wrapped to.
    assert "Created a local encryption key at " in stream.getvalue()
    assert str(at_rest.local_key_path()) in stream.getvalue()
    assert str(at_rest.device_key_path()) in stream.getvalue()


def test_no_key_is_created_under_the_opt_out(root, monkeypatch, tmp_path):
    from openadapt_flow import at_rest

    monkeypatch.setenv("OPENADAPT_FLOW_AT_REST_HOME", str(tmp_path / "home"))
    stream = io.StringIO()
    view = PlainView(stream)
    with view.collect_notices():
        run_story(FakeEngine(root), view, next_steps=_steps, clock=_clock())
    assert not at_rest.local_key_path().exists()
    assert "encryption key" not in stream.getvalue()


# ---------------------------------------------------------------------------
# Image protocol detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("environ", "isatty", "expected"),
    [
        ({"TERM_PROGRAM": "iTerm.app"}, True, ITERM2),
        ({"LC_TERMINAL": "iTerm2"}, True, ITERM2),
        ({"TERM_PROGRAM": "WezTerm"}, True, ITERM2),
        ({"KITTY_WINDOW_ID": "3"}, True, KITTY),
        ({"TERM": "xterm-kitty"}, True, KITTY),
        ({"TERM_PROGRAM": "ghostty"}, True, KITTY),
        ({"TERM": "xterm-ghostty"}, True, KITTY),
        ({"TERM_PROGRAM": "Apple_Terminal"}, True, None),
        ({"TERM_PROGRAM": "vscode"}, True, None),
        ({}, True, None),
        ({"TERM_PROGRAM": "iTerm.app"}, False, None),
        ({"TERM_PROGRAM": "iTerm.app", "TMUX": "/tmp/tmux"}, True, None),
        ({"TERM_PROGRAM": "iTerm.app", "TERM": "screen-256color"}, True, None),
        ({"TERM_PROGRAM": "iTerm.app", "OPENADAPT_INLINE_IMAGES": "0"}, True, None),
        ({"TMUX": "x", "OPENADAPT_INLINE_IMAGES": "kitty"}, True, KITTY),
    ],
)
def test_image_protocol_detection(environ, isatty, expected):
    assert detect_image_protocol(environ, isatty=isatty) == expected


def test_kitty_payload_is_chunked():
    sequence = image_sequence(b"x" * 10_000, KITTY, cols=40, rows=10)
    chunks = sequence.split("\x1b\\")[:-1]
    assert len(chunks) > 1
    assert chunks[0].startswith("\x1b_Ga=T,f=100,q=2,c=40,r=10,m=1;")
    assert chunks[-1].startswith("\x1b_Gm=0;")
    assert all(len(chunk.split(";", 1)[1]) <= 4096 for chunk in chunks)


def test_beside_image_reserves_rows_and_offsets_the_text():
    block = beside_image("IMG", ["one", "two"], indent=3, cols=40, rows=4)
    assert block.startswith("\n" * 4 + "\x1b[4A")
    assert "\x1b7\x1b[3CIMG\x1b8" in block
    assert "\r\x1b[46Cone\n" in block and "\r\x1b[46Ctwo\n" in block
    assert block.count("\n") == 4 + 4


# ---------------------------------------------------------------------------
# The demonstration ticker reads the recorder's own log
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "label"),
    [
        (
            {"kind": "click", "structural": {"name": "Save Encounter"}},
            "Click Save Encounter",
        ),
        ({"kind": "click"}, "Click"),
        ({"kind": "type", "text": "hello"}, 'Type "hello"'),
        ({"kind": "key", "key": "Enter"}, "Press Enter"),
        ({"kind": "scroll"}, None),
    ],
)
def test_demo_step_labels(event, label):
    assert demo_step_label(event) == label


def test_step_ticker_reports_steps_as_the_recorder_writes_them(tmp_path):
    events = tmp_path / "events.jsonl"
    seen: list[DemoStep] = []
    lines = [
        {"kind": "click", "structural": {"name": "Open"}, "t": 0.31},
        {"kind": "type", "text": "note", "t": 1.02},
    ]

    def write():
        for line in lines:
            with events.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line))
                time.sleep(0.02)  # a half-written line must wait
                handle.write("\n")
            time.sleep(0.08)

    with StepTicker(events, seen.append) as ticker:
        writer = threading.Thread(target=write)
        writer.start()
        time.sleep(0.05)
        writer.join()
    assert [(s.label, s.seconds) for s in seen] == [
        ("Click Open", 0.31),
        ('Type "note"', 1.02),
    ]
    assert ticker.steps == seen


# ---------------------------------------------------------------------------
# Printed commands work as printed
# ---------------------------------------------------------------------------


def _seal_available() -> bool:
    try:
        from openadapt_flow.offline_seal import seal_run  # noqa: F401
    except ImportError:
        return False
    return True


def _console_script() -> Optional[str]:
    candidate = Path(sys.executable).parent / (
        "openadapt.exe" if os.name == "nt" else "openadapt"
    )
    return str(candidate) if candidate.exists() else None


def test_uvx_spelling_reaches_every_printed_command(root, monkeypatch):
    monkeypatch.setattr(sys, "prefix", "/Users/x/.cache/uv/archive-v0/abc")
    outcome = Outcome(root=root, seal=SealFacts("k", 5))
    steps = next_steps(cli._hint, cli._shell_quote, True)(outcome)
    py = f"{sys.version_info[0]}.{sys.version_info[1]}"
    for command in (steps.verify, steps.record, steps.agent):
        assert command.startswith(f"uvx --python {py} openadapt "), command


def test_every_printed_command_answers_help_in_a_fresh_environment(tmp_path):
    """Run each next-step command as printed, plus --help, in a new process.

    The agent command is the one the launcher would print with the installed
    agent. The Seal check is printed only when the engine can sign Seals, so
    it is tested only then.
    """
    pytest.importorskip("openadapt_flow", reason="openadapt-flow not installed")
    executable = _console_script()
    if executable is None:
        pytest.skip("the openadapt console script is not installed")
    root = tmp_path / "openadapt-quickstart"
    root.mkdir()
    outcome = Outcome(root=root, seal=SealFacts("k", 1) if _seal_available() else None)
    steps = next_steps(cli._hint, cli._shell_quote, cli._agent_serves_tutorial())(
        outcome
    )
    commands = [steps.record, steps.agent] + ([steps.verify] if steps.verify else [])
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "OPENADAPT_SEAL_HOME": str(tmp_path / "seal"),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    }
    for command in commands:
        argv = shlex.split(command)
        assert argv[0] == "openadapt", command
        result = subprocess.run(
            [executable, *argv[1:], "--help"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, (command, result.stdout, result.stderr)
        assert "usage" in (result.stdout + result.stderr).lower(), command


def test_seal_verify_checks_each_sealed_run_in_a_folder(tmp_path, monkeypatch):
    if not _seal_available():
        pytest.skip("this openadapt-flow cannot sign offline Seals")
    from openadapt_flow.offline_seal import seal_run

    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    root = tmp_path / "openadapt-quickstart"
    for index in (1, 2):
        run = root / f"run-{index}"
        run.mkdir(parents=True)
        (run / "report.json").write_text(
            json.dumps({"execution_outcome": "VERIFIED", "n": index}),
            encoding="utf-8",
        )
        seal_run(run, home=tmp_path / "seal", enqueue=False)
    (root / "bundle").mkdir()

    result = CliRunner().invoke(cli.main, ["seal", "verify", str(root)])
    assert result.exit_code == 0, result.output
    assert "run-1  valid" in result.output and "2 of 2 valid" in result.output

    (root / "run-2" / "report.json").write_text("{}", encoding="utf-8")
    result = CliRunner().invoke(cli.main, ["seal", "verify", str(root)])
    assert result.exit_code == 1
    assert "run-2  not valid" in result.output and "1 of 2 valid" in result.output


def test_seal_verify_names_a_folder_with_no_sealed_run(tmp_path):
    result = CliRunner().invoke(cli.main, ["seal", "verify", str(tmp_path)])
    assert result.exit_code == 1
    assert "no sealed run" in result.output


# ---------------------------------------------------------------------------
# The real thing
# ---------------------------------------------------------------------------


@pytest.mark.real_quickstart
@pytest.mark.skipif(
    os.environ.get("OPENADAPT_REAL_QUICKSTART") != "1",
    reason="set OPENADAPT_REAL_QUICKSTART=1 to drive a real browser (~30 s)",
)
def test_real_quickstart(tmp_path, monkeypatch, capfd):
    from openadapt.quickstart.engine import FlowEngine

    monkeypatch.chdir(tmp_path)
    # Flow's default: checkpoints encrypted under a new local key.
    monkeypatch.delenv("OPENADAPT_FLOW_PLAINTEXT_AT_REST", raising=False)
    monkeypatch.setenv("OPENADAPT_FLOW_AT_REST_HOME", str(tmp_path / "at-rest"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    # The device key goes to a file here, never to the OS keychain.
    monkeypatch.setenv("OPENADAPT_FLOW_KEY_STORE", "file")
    root = tmp_path / "openadapt-quickstart"
    stream = io.StringIO()
    outcome = run_quickstart(
        root,
        visible=False,
        break_it=True,
        verbose=False,
        steps=_steps,
        stream=stream,
        interactive=False,
    )
    output = stream.getvalue()
    # The quickstart runs the program on FlowEngine.inputs (one new note).
    runs = len(FlowEngine.inputs)
    assert outcome.checked == runs, output
    assert outcome.model_calls == 0
    assert outcome.broken is not None and outcome.broken.stopped
    assert outcome.broken.new_records == 0
    assert outcome.broken.transaction_outcome == "RECONCILIATION_REQUIRED"
    assert f"{runs} of {runs} saved and checked" in output
    from openadapt_flow import sealed_set

    notes = []
    for index in range(1, runs + 1):
        run_dir = root / f"run-{index}"
        # Encrypted by default: read in memory, receipts beside it.
        report = json.loads(sealed_set.read_bytes(run_dir / "report.json"))
        assert report["execution_outcome"] == "VERIFIED"
        assert report["params"]["note"] in output
        notes.append(report["params"]["note"].encode("utf-8"))
        assert sealed_set.is_sealed(run_dir)
        assert (run_dir / "seal.json").is_file()
    checkpoints = root / "run-1" / "checkpoints"
    assert list(checkpoints.glob("*.enc"))
    assert not list(checkpoints.glob("*.json"))
    # Only ciphertext screenshots, frames and typed values on disk. The
    # receipt files are plaintext by design; the receipt pages embed the
    # report (logged); the summary page leaves the screenshots out.
    plain_ok = {"seal.html", "receipt.png"}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if path.name not in plain_ok:
            assert not data.startswith(b"\x89PNG"), path
            assert not data.startswith(b"\xff\xd8\xff"), path
        if path.name != "seal.html":
            for note in notes:
                assert note not in data, path
    for folder in ("recording", "bundle", "run-1", "run-broken"):
        assert sealed_set.is_sealed(root / folder), folder
    assert "Created a local encryption key" in output
    # Neither the key notice nor a plaintext notice reached the terminal.
    err = capfd.readouterr().err
    assert "Created a local encryption key" not in err
    assert "in plaintext at" not in err


def test_the_seal_pages_are_run_ones_and_the_stopped_runs(tmp_path, monkeypatch):
    """Runs finish in any order; the page the finale opens is the first run's."""

    from openadapt import seal_view
    from openadapt.quickstart.engine import FlowEngine

    written = []
    monkeypatch.setattr(seal_view, "bundle_graph", lambda bundle: {"nodes": []})
    monkeypatch.setattr(
        seal_view,
        "write_seal_page",
        lambda run_dir, **kw: (
            written.append((run_dir.name, kw["about"]["kicker"]))
            or run_dir / "seal.html"
        ),
    )
    runs = [_run(tmp_path, index) for index in (3, 2, 1, 5, 4)]
    broken = _broken(tmp_path)
    for run_dir in [run.run_dir for run in runs] + [broken.run_dir]:
        run_dir.mkdir(parents=True)
        (run_dir / "seal.json").write_text("{}")
    engine = FlowEngine(tmp_path, visible=False)
    pages = engine.seal_pages(runs, broken)
    assert pages.verified == tmp_path / "run-1" / "seal.html"
    assert pages.broken == tmp_path / "run-broken" / "seal.html"
    assert written == [
        ("run-1", "Run 1 of 5"),
        ("run-broken", "The run with the failed save"),
    ]


def test_an_unsealed_stopped_run_gets_no_page(tmp_path, monkeypatch):
    from openadapt import seal_view
    from openadapt.quickstart.engine import FlowEngine

    monkeypatch.setattr(seal_view, "bundle_graph", lambda bundle: {"nodes": []})
    monkeypatch.setattr(
        seal_view, "write_seal_page", lambda run_dir, **kw: run_dir / "seal.html"
    )
    pages = FlowEngine(tmp_path, visible=False).seal_pages([], _broken(tmp_path))
    assert pages.broken is None and pages.verified is None


def test_sealing_the_stopped_run_signs_its_report(tmp_path, monkeypatch):
    from openadapt.quickstart.engine import FlowEngine

    sealed = []

    def seal_run(run_dir, **kw):
        sealed.append(run_dir.name)
        (run_dir / "seal.json").write_text("{}")

    monkeypatch.setattr("openadapt_flow.offline_seal.seal_run", seal_run)
    broken = _broken(tmp_path)
    broken.run_dir.mkdir(parents=True)
    assert FlowEngine(tmp_path, visible=False).seal_broken(broken) is True
    assert sealed == ["run-broken"]


# ---------------------------------------------------------------------------
# Review fixes: encryption at rest, recorded compile time, the Seal page
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"OPENADAPT_BUNDLE_KEY": "k"},
        {"OPENADAPT_FLOW_AT_REST": "strict"},
        {"OPENADAPT_FLOW_PLAINTEXT_AT_REST": "operator reason"},
    ],
)
def test_the_quickstart_keeps_flows_encryption_at_rest(monkeypatch, environ):
    """Checkpoints are encrypted by Flow's default; the quickstart sets no opt-out."""
    names = (
        "OPENADAPT_BUNDLE_KEY",
        "OPENADAPT_RECORDING_KEY",
        "OPENADAPT_FLOW_AT_REST",
        "OPENADAPT_FLOW_PLAINTEXT_AT_REST",
        "OPENADAPT_FLOW_SCRUB",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    before = {name: os.environ.get(name) for name in names}
    with cli._synthetic_scrub_off():
        inside = {name: os.environ.get(name) for name in names}
    assert inside.pop("OPENADAPT_FLOW_SCRUB") == "off"
    before_scrub = before.pop("OPENADAPT_FLOW_SCRUB")
    assert inside == before
    # Restored afterwards: the scrub-off never outlives the quickstart.
    assert os.environ.get("OPENADAPT_FLOW_SCRUB") == before_scrub


def test_a_recorded_demonstration_times_the_compile_alone():
    from openadapt.quickstart.scene import OPENEMR
    from openadapt.quickstart.view import compile_seconds

    outcome = Outcome(root=Path("qs"))
    outcome.act1_seconds = 0.5
    outcome.demo_steps = [DemoStep(label="Click Save", seconds=36.8)]
    assert compile_seconds(outcome, OPENEMR) == 0.5
    outcome.act1_seconds = 2.0
    outcome.demo_steps = [DemoStep(label="Click Save", seconds=1.6)]
    assert compile_seconds(outcome) == pytest.approx(0.4)


def test_a_captured_stream_never_opens_the_seal_page():
    """Under pytest -s stdout can be a terminal; the stream decides, not stdout."""

    import openadapt.quickstart as quickstart

    assert quickstart._shown(io.StringIO(), lambda: True) is False
    assert quickstart._shown(_Terminal(), lambda: True) is True
    assert quickstart._shown(_Terminal(), lambda: False) is False
