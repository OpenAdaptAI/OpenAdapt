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
from openadapt.quickstart.view import Marks, PlainView, RichView, make_view

SNAPSHOT = Path(__file__).parent / "snapshots" / "quickstart_plain.snapshot"

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
        screen_text="Encounter saved — Synthetic follow-up in two weeks",
        screenshot=None,
        new_records=0,
        effects_required=2,
        effects_refuted=1,
        halt_reason="record_written refuted",
        model_calls=0,
        seconds=7.7,
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
        return _broken(self.root, **self._broken)

    def seal(self, runs):
        self.calls.append("seal")
        if self._seal is None:
            return None
        return SealFacts(self._seal.key_id, sum(1 for run in runs if run.checked))

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
    assert engine.calls == [
        "enter",
        "start workers",
        "record",
        "compile",
        *(f"run {index}" for index in range(1, 6)),
        "break",
        "seal",
        "page",
        "exit",
    ]
    acts = [output.index(f"\n{n}  ") for n in (1, 2, 3)]
    assert acts == sorted(acts)
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


def test_finale_is_at_most_twelve_lines_with_exactly_two_next_steps(root):
    output, _ = _plain(FakeEngine(root))
    finale = output[output.index("\nReceipt\n") + 1 :].rstrip("\n").splitlines()
    assert len(finale) <= 12, finale
    assert [line for line in finale if line.startswith(("Try it", "Give your"))] == [
        "Try it on your own app: openadapt flow record --backend web --url "
        "https://your-app.example --out my-task",
        "Give your agent this as a tool: openadapt agent serve --allow-run",
    ]
    assert sum(1 for line in finale if line.startswith("Details: ")) == 1


def test_no_break_skips_act_three(root):
    engine = FakeEngine(root)
    output, outcome = _plain(engine, break_it=False)
    assert "Break it" not in output
    assert "break" not in engine.calls
    assert "page" not in engine.calls
    assert outcome.broken is None
    assert "Receipt" in output


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

    assert "Compiled into a program: 9 steps." in output
    assert "Click Go" in output and "3.8s" in output
    assert "11.2s" in output  # run 2's own time
    assert "■ Stopped before saving" in output
    assert "4 of 5 saved and checked · 3 AI calls · $0.04" in output
    assert "5 of 5" not in output
    assert "0 AI calls" not in output
    assert "Seal       seal-ed25519-0123456789abcdef · 4 reports signed" in output
    assert "Cost       3 AI calls · $0.04 · " in output
    assert outcome.checked == 4


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
    assert "Stopped before reporting success" not in missed
    assert "did not stop" in missed


def test_an_engine_without_run_inputs_does_not_claim_new_notes(root):
    output, _ = _plain(FakeEngine(root, note_param=False))
    assert "new notes" not in output
    assert "2  Run it · the program 5 times, all at once" in output


@pytest.mark.parametrize(
    ("parallel", "subtitle"),
    [(5, "all at once"), (3, "3 at a time"), (1, "new notes\n")],
)
def test_act_two_says_how_many_run_at_once(root, parallel, subtitle):
    output, _ = _plain(FakeEngine(root, parallel=parallel))
    assert subtitle in output


def test_no_seal_means_no_seal_line_and_no_verify_command(root):
    output, _ = _plain(FakeEngine(root, seal=None))
    assert "Seal" not in output
    assert "seal verify" not in output


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
    assert "Break it" in stream.getvalue()


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


def _rich(root, environ, **engine_kwargs) -> str:
    stream = _Terminal()
    view = RichView(stream, environ=environ, width=110)
    run_story(
        FakeEngine(root, **engine_kwargs),
        view,
        next_steps=_steps,
        clock=_clock(),
    )
    return stream.getvalue()


def test_terminal_finale_fits_twelve_lines_with_every_optional_line(root, monkeypatch):
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
    RichView(stream, environ={}, width=110).finale(outcome, steps)
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert len(lines) <= 12, lines


def test_terminal_view_renders_color_boxes_and_the_comparison(root):
    output = _rich(root, {"TERM_PROGRAM": "Apple_Terminal"})
    assert "\x1b[" in output
    assert "╭─ Receipt" in output
    assert "What the screen showed" in output
    assert "Encounter saved — Synthetic" in output
    assert "What the database showed" in output
    assert "0 new records" in output
    assert "A screen-trusting bot" in output and "✓ Done" in output
    assert "■ Stopped before reporting success" in output
    assert "\x1b]1337;" not in output and "\x1b_G" not in output


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
    assert "What the database showed" in output
    # The text box is the fallback, not drawn as well.
    assert "╭─ What the screen showed" not in output


def test_terminal_view_falls_back_when_the_screenshot_is_missing(root):
    output = _rich(root, {"TERM_PROGRAM": "iTerm.app"}, broken={"screenshot": None})
    assert "\x1b]1337;" not in output
    assert "╭─ What the screen showed" in output


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
        import openadapt_flow.offline_seal  # noqa: F401
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
    from openadapt_flow.offline_seal import seal_run, signer_for

    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    root = tmp_path / "openadapt-quickstart"
    signer = signer_for(tmp_path / "seal")
    for index in (1, 2):
        run = root / f"run-{index}"
        run.mkdir(parents=True)
        (run / "report.json").write_text(
            json.dumps({"execution_outcome": "VERIFIED", "n": index}),
            encoding="utf-8",
        )
        seal_run(run, home=tmp_path / "seal", enqueue=False, signer=signer)
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
def test_real_quickstart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
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
    assert outcome.checked == len(RUN_NOTES), output
    assert outcome.model_calls == 0
    assert outcome.broken is not None and outcome.broken.stopped
    assert outcome.broken.new_records == 0
    assert f"{len(RUN_NOTES)} of {len(RUN_NOTES)} saved and checked" in output
    for index in range(1, len(RUN_NOTES) + 1):
        report = json.loads((root / f"run-{index}" / "report.json").read_text())
        assert report["execution_outcome"] == "VERIFIED"
        assert report["params"]["note"] in output
