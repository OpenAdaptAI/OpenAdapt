"""The quickstart on an unready screen, a wide terminal and an old Python.

The founder's first run in Terminal.app halted at step one: the visible
browser window was covered and drew no frames, so the readiness gate never
saw a settled screen. These tests pin what the quickstart does about that
(one more try in a new folder, then a plain explanation, never the "stopped,
but not at the check" line), the width cap, and the rerun under ``uvx`` on a
Python the engine doesn't support.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from test_quickstart import (
    FakeEngine,
    _broken,
    _clock,
    _plain,
    _rich,
    _run,
    _steps,
    _visible,
)

from openadapt import cli
from openadapt.quickstart.engine import (
    FlowEngine,
    not_ready_explanation,
    starting_state_not_ready,
)
from openadapt.quickstart.images import ITERM2, KITTY, image_protocol
from openadapt.quickstart.story import run_story
from openadapt.quickstart.view import MAX_WIDTH, RichView, hyperlinks_supported

NOT_READY = (
    "The screen never stopped changing (still loading or animating) within "
    "10.0s - refusing to act on a mid-transition frame; run aborted (starting "
    "state not ready)."
)


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path / "openadapt-quickstart"


# ---------------------------------------------------------------------------
# What counts as "never got a ready screen"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("execution", "transaction", "reason", "expected"),
    [
        ("HALTED", "HALTED_BEFORE_EFFECT", NOT_READY, True),
        # Only a stop that proves nothing was written is retried.
        ("HALTED", "RECONCILIATION_REQUIRED", NOT_READY, False),
        ("HALTED", "HALTED_BEFORE_EFFECT", "record_written refuted", False),
        ("VERIFIED", "VERIFIED", "", False),
    ],
)
def test_starting_state_not_ready(execution, transaction, reason, expected):
    assert starting_state_not_ready(execution, transaction, reason) is expected


def test_the_explanation_names_the_window_only_when_one_was_shown():
    assert "--watch" in not_ready_explanation(True)
    assert "window" not in not_ready_explanation(False)
    for visible in (True, False):
        assert "saved nothing" in not_ready_explanation(visible)


# ---------------------------------------------------------------------------
# One more try, in a new folder
# ---------------------------------------------------------------------------


def _engine(root: Path, results: dict[str, Any]) -> tuple[FlowEngine, list[str]]:
    engine = FlowEngine(root, visible=True)
    engine.workflow = object()
    folders: list[str] = []

    def run(index, note, folder=None):
        folders.append(folder)
        return results[folder]

    engine.run = run  # type: ignore[method-assign]
    engine.note_param = lambda: True  # type: ignore[method-assign]
    return engine, folders


def test_a_run_that_never_got_a_ready_screen_runs_once_more(root):
    unready = _run(root, 1, checked=False, not_ready=True)
    retried = _run(root, 1, run_dir=root / "run-1-retry")
    engine, folders = _engine(root, {"run-1": unready, "run-1-retry": retried})
    finished = []
    engine.run_all(["note"], finished.append)
    assert folders == ["run-1", "run-1-retry"]
    # Only the second try is reported, marked as a repeat.
    assert finished == [retried] and finished[0].repeated


def test_the_retry_happens_once(root):
    first = _run(root, 1, checked=False, not_ready=True)
    second = _run(root, 1, checked=False, not_ready=True, run_dir=root / "x")
    engine, folders = _engine(root, {"run-1": first, "run-1-retry": second})
    finished = []
    engine.run_all(["note"], finished.append)
    assert folders == ["run-1", "run-1-retry"]
    assert finished == [second]


def test_a_stop_for_another_reason_is_not_retried(root):
    stopped = _run(root, 1, checked=False)
    engine, folders = _engine(root, {"run-1": stopped})
    finished = []
    engine.run_all(["note"], finished.append)
    assert folders == ["run-1"] and finished == [stopped]


def test_the_break_run_gets_one_more_try(root, monkeypatch):
    engine = FlowEngine(root, visible=False)
    unready = _broken(root, not_ready=True, stop_attributed=False, seconds=10.0)
    caught = _broken(root, run_dir=root / "run-broken-retry", seconds=7.0)
    tries = iter([unready, caught])
    seen: list[Path] = []

    def once(run_dir):
        seen.append(run_dir)
        return next(tries)

    monkeypatch.setattr(engine, "_break_once", once)
    result = engine.break_it()
    assert seen == [root / "run-broken", root / "run-broken-retry"]
    assert result is caught and result.seconds == 17.0


# ---------------------------------------------------------------------------
# What the views say
# ---------------------------------------------------------------------------


def test_no_ready_screen_explains_and_skips_the_break(root):
    explanation = not_ready_explanation(True)
    unready = {"checked": False, "not_ready": True, "explanation": explanation}
    engine = FakeEngine(root, runs={index: unready for index in range(1, 6)})
    text, outcome = _plain(engine)
    assert explanation in text
    assert "Skipped the last part" in text
    assert "Stopped before its first click" in text
    assert outcome.broken is None
    assert "not at the check" not in text


def test_an_unready_break_run_says_why_in_plain_words(root):
    explanation = not_ready_explanation(False)
    engine = FakeEngine(
        root,
        broken={
            "not_ready": True,
            "stop_attributed": False,
            "screen_showed_success": False,
            "screen_text": None,
            "transaction_outcome": "HALTED_BEFORE_EFFECT",
            "explanation": explanation,
        },
    )
    text, _outcome = _plain(engine)
    assert explanation in text
    assert "not at the check" not in text
    assert "never reached: the run stopped before its first click" in text
    for terminal in ({"TERM_PROGRAM": "Apple_Terminal"}, {"TERM_PROGRAM": "WezTerm"}):
        rich = _visible(
            _rich(
                root,
                terminal,
                broken={
                    "not_ready": True,
                    "stop_attributed": False,
                    "screen_showed_success": False,
                    "screen_text": None,
                    "explanation": explanation,
                },
            )
        )
        assert "not at the check" not in rich
        assert "never finished loading" in " ".join(rich.split())


# ---------------------------------------------------------------------------
# Width: a wide window keeps timings next to their rows
# ---------------------------------------------------------------------------


class _WideTerminal(io.StringIO):
    encoding = "utf-8"

    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize("columns", [120, 250])
def test_a_wide_terminal_draws_at_most_max_width(root, monkeypatch, columns):
    monkeypatch.setenv("COLUMNS", str(columns))
    stream = _WideTerminal()
    view = RichView(stream, environ={}, animate=False)
    assert view.console.width == MAX_WIDTH
    run_story(FakeEngine(root), view, next_steps=_steps, clock=_clock())
    lines = _visible(stream.getvalue()).splitlines()
    long = [line for line in lines if len(line) > MAX_WIDTH]
    # Only an unbroken file:// address may run past the cap.
    assert all("file://" in line for line in long), long
    # Each step's timing sits right after its text, not at the right edge.
    for title in ("Show it once", "Turn it into a program"):
        line = next(line for line in lines if title in line)
        assert line.rstrip().endswith("s") and len(line.rstrip()) < 80, line


# ---------------------------------------------------------------------------
# Images only where the terminal answers; links where it supports them
# ---------------------------------------------------------------------------


def test_an_image_is_used_only_when_the_terminal_answers():
    asked: list[str] = []

    def answers(protocol: str) -> bool:
        asked.append(protocol)
        return protocol == KITTY

    assert (
        image_protocol({"TERM_PROGRAM": "iTerm.app"}, isatty=True, answers=answers)
        is None
    )
    assert (
        image_protocol({"TERM_PROGRAM": "ghostty"}, isatty=True, answers=answers)
        == KITTY
    )
    assert asked == [ITERM2, KITTY]
    # An explicit choice is used as given, without asking.
    assert (
        image_protocol(
            {"OPENADAPT_INLINE_IMAGES": "iterm2"},
            isatty=True,
            answers=lambda _p: pytest.fail("asked"),
        )
        == ITERM2
    )
    assert image_protocol({"TERM_PROGRAM": "Apple_Terminal"}, isatty=True) is None


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"TERM_PROGRAM": "Apple_Terminal"}, False),
        ({"TERM_PROGRAM": "WezTerm"}, True),
        ({"TERM_PROGRAM": "iTerm.app"}, True),
        ({"KITTY_WINDOW_ID": "1"}, True),
        ({"TERM_PROGRAM": "iTerm.app", "TMUX": "x"}, False),
        ({"VTE_VERSION": "7600"}, True),
        ({}, False),
    ],
)
def test_hyperlinks_supported(environ, expected):
    assert hyperlinks_supported(environ) is expected


# ---------------------------------------------------------------------------
# The alias notice and the rerun on an unsupported Python
# ---------------------------------------------------------------------------


def test_quickstart_prints_no_alias_notice():
    from openadapt.cli_tasks import legacy_names

    assert legacy_names(["quickstart"]) is None
    assert legacy_names(["quickstart", "--break-it"]) is None


def _uvx(monkeypatch, version="1.24.0"):
    monkeypatch.setattr(sys, "prefix", "/Users/x/.cache/uv/archive-v0/abc")
    monkeypatch.setattr(cli, "__version__", version)
    monkeypatch.setattr(cli, "_python_minor", lambda: (3, 14))
    monkeypatch.setenv("UV", "/opt/uv")
    monkeypatch.delenv("OPENADAPT_PYTHON_RERUN", raising=False)
    monkeypatch.setattr(sys, "argv", ["/x/bin/openadapt", "quickstart", "--break-it"])


def test_under_uvx_an_unsupported_python_reruns_on_3_12(monkeypatch):
    _uvx(monkeypatch)
    calls: list[tuple[list[str], dict]] = []

    def call(argv, env):
        calls.append((argv, env))
        return 0

    monkeypatch.setattr("subprocess.call", call)
    result = CliRunner().invoke(cli.main, ["quickstart", "--break-it"])
    assert result.exit_code == 0
    argv, env = calls[0]
    assert argv == [
        "/opt/uv",
        "tool",
        "run",
        "--python",
        "3.12",
        "openadapt@1.24.0",
        "quickstart",
        "--break-it",
    ]
    assert env["OPENADAPT_PYTHON_RERUN"] == "1"
    assert "uvx --python 3.12 openadapt@1.24.0 quickstart --break-it" in result.output


def test_the_rerun_exit_code_is_this_commands(monkeypatch):
    _uvx(monkeypatch)
    monkeypatch.setattr("subprocess.call", lambda argv, env: 3)
    assert CliRunner().invoke(cli.main, ["quickstart"]).exit_code == 3


def test_a_development_build_reruns_latest(monkeypatch):
    _uvx(monkeypatch, version="1.24.0.dev3")
    argv, shown = cli._rerun_command()
    assert "openadapt@latest" in argv and "openadapt@latest" in shown


def test_the_rerun_never_loops(monkeypatch):
    _uvx(monkeypatch)
    monkeypatch.setenv("OPENADAPT_PYTHON_RERUN", "1")
    monkeypatch.setattr("subprocess.call", lambda *a, **k: pytest.fail("reran"))
    result = CliRunner().invoke(cli.main, ["quickstart"])
    assert result.exit_code == 1
    assert cli.QUICKSTART_COMMAND in result.output


def test_outside_uvx_the_fix_leads_with_the_one_line_command(monkeypatch):
    monkeypatch.setattr(cli, "_python_minor", lambda: (3, 14))
    monkeypatch.setattr(sys, "prefix", "/usr/local")
    monkeypatch.setattr("subprocess.call", lambda *a, **k: pytest.fail("reran"))
    result = CliRunner().invoke(cli.main, ["quickstart"])
    assert result.exit_code == 1
    lines = result.output.splitlines()
    assert f"  {cli.QUICKSTART_COMMAND}" in lines
    assert "uv tool install --python 3.12 openadapt" in result.output
