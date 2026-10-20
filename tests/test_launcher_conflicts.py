"""Another `openadapt` on PATH that shadows this one, or is newer, is named.

`uv tool install openadapt` puts the launcher in ~/.local/bin, but a Conda
`bin` earlier on PATH can keep running an old 1.x launcher, which fails with
"invalid choice" on every new command. When this launcher runs (`--version`,
an unknown command, or under uvx), it names the other install and the fix.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from openadapt import cli
from openadapt.version import __version__


def _launcher(folder: Path, version: str, *, python: bool = True) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "openadapt"
    path.write_text(f"#!/bin/sh\necho 'openadapt, version {version}'\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    if python:
        (folder / "python").write_text("")
    return path


def _run(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True, timeout=15)


pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX shell stand-ins")


def test_path_launchers_follow_path_order_without_duplicates(tmp_path) -> None:
    first = _launcher(tmp_path / "conda" / "bin", "1.5.1")
    second = _launcher(tmp_path / "local" / "bin", __version__)
    path = os.pathsep.join([str(first.parent), str(second.parent), str(first.parent)])
    assert cli._path_launchers({"PATH": path}) == [first, second]


def test_an_older_launcher_first_on_path_is_named_with_the_fix(tmp_path) -> None:
    old = _launcher(tmp_path / "conda" / "bin", "1.5.1")
    lines = cli._launcher_conflicts([old], run=_run)
    assert lines == [
        f"Your shell runs openadapt 1.5.1 at {old} before this one ({__version__}). "
        f"To remove it: {old.parent / 'python'} -m pip uninstall openadapt "
        "openadapt-flow, then run: hash -r"
    ]


def test_without_a_python_beside_it_the_fix_names_the_environment(tmp_path) -> None:
    old = _launcher(tmp_path / "env" / "bin", "1.5.1", python=False)
    (line,) = cli._launcher_conflicts([old], run=_run)
    assert line.endswith(
        f"pip uninstall openadapt openadapt-flow from {old.parent.parent}, "
        "then run: hash -r"
    )


def test_a_newer_launcher_later_on_path_is_named(tmp_path, monkeypatch) -> None:
    here = Path(sys.prefix) / "bin"
    newer = _launcher(tmp_path / "new" / "bin", "99.0.0")
    lines = cli._launcher_conflicts([here / "openadapt", newer], run=_run)
    assert lines == [
        f"A newer openadapt, 99.0.0, is at {newer}; this one is {__version__}."
    ]


def test_copies_of_this_version_are_fine(tmp_path) -> None:
    same = _launcher(tmp_path / "local" / "bin", __version__)
    assert cli._launcher_conflicts([same], run=_run) == []
    assert cli._launcher_conflicts([], run=_run) == []


def test_version_prints_the_version_then_any_conflict(monkeypatch) -> None:
    monkeypatch.setattr(
        cli, "_launcher_conflicts", lambda: ["Your shell runs openadapt 1.5.1"]
    )
    result = CliRunner().invoke(cli.main, ["--version"])
    assert result.exit_code == 0
    assert result.output.splitlines()[0] == f"openadapt, version {__version__}"
    assert "Your shell runs openadapt 1.5.1" in result.output


def test_an_unknown_command_names_a_conflict(monkeypatch) -> None:
    monkeypatch.setattr(
        cli, "_launcher_conflicts", lambda: ["A newer openadapt, 99.0.0"]
    )
    result = CliRunner().invoke(cli.main, ["no-such-verb"])
    assert result.exit_code == 2
    assert "A newer openadapt, 99.0.0" in result.output
    assert "No such command 'no-such-verb'" in result.output


def test_a_failing_path_check_never_breaks_version(monkeypatch) -> None:
    def boom():
        raise OSError("unreadable PATH entry")

    monkeypatch.setattr(cli, "_launcher_conflicts", boom)
    result = CliRunner().invoke(cli.main, ["--version"])
    assert result.exit_code == 0
