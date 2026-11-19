"""The engine starts its child processes without forking itself."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from openadapt_desktop import child_process

ENGINE = Path(child_process.__file__).resolve().parent
STARTERS = {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
OS_STARTERS = {"system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp"}


def _direct_starts(path: Path) -> list[str]:
    found = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    # A type such as ``subprocess.Popen[str]`` in an annotation starts nothing.
    annotations = [
        getattr(node, name)
        for node in ast.walk(tree)
        for name in ("annotation", "returns")
        if getattr(node, name, None) is not None
    ]
    in_annotation = {id(inner) for outer in annotations for inner in ast.walk(outer)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
            continue
        if id(node) in in_annotation:
            continue
        if (node.value.id == "subprocess" and node.attr in STARTERS) or (
            node.value.id == "os" and (node.attr in OS_STARTERS or node.attr.startswith("spawn"))
        ):
            found.append(f"{path.relative_to(ENGINE)}:{node.lineno} {node.value.id}.{node.attr}")
    return found


def test_engine_modules_start_processes_only_through_child_process() -> None:
    """A direct subprocess.run or Popen in the engine would fork it on macOS."""

    offenders = [
        entry
        for path in sorted(ENGINE.rglob("*.py"))
        if path.name != "child_process.py"
        for entry in _direct_starts(path)
    ]
    assert offenders == [], "start these through openadapt_desktop.child_process: " + ", ".join(
        offenders
    )


@pytest.fixture
def fork_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32" or not getattr(subprocess, "_USE_POSIX_SPAWN", False):
        pytest.skip("this platform does not choose between fork and posix_spawn")

    def refuse(args, *_rest, **_named):
        raise AssertionError(f"{args!r} would fork the engine")

    monkeypatch.setattr(subprocess, "_fork_exec", refuse)


def test_python_can_spawn_without_forking_on_macos() -> None:
    if sys.platform == "darwin":
        assert subprocess._USE_POSIX_SPAWN


def test_run_starts_a_child_without_forking(fork_refused: None) -> None:
    done = child_process.run(
        [sys.executable, "-c", "print('ready')"], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0 and done.stdout.strip() == "ready"
    with pytest.raises(AssertionError, match="would fork the engine"):
        subprocess.run([sys.executable, "-c", "pass"])


def test_popen_with_pipes_starts_a_child_without_forking(fork_refused: None) -> None:
    process = child_process.popen(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, _err = process.communicate("signed", timeout=60)
    assert process.returncode == 0 and out.strip() == "SIGNED"


def test_a_program_named_without_a_directory_is_found_on_path(fork_refused: None) -> None:
    done = child_process.run(["sh", "-c", "echo found"], capture_output=True, text=True, timeout=60)
    assert done.stdout.strip() == "found"
    environment = {**os.environ, "PATH": "/bin:/usr/bin"}
    done = child_process.run(
        ["sh", "-c", "echo found"], capture_output=True, text=True, timeout=60, env=environment
    )
    assert done.stdout.strip() == "found"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no fork")
@pytest.mark.parametrize("option", ["cwd", "start_new_session", "preexec_fn", "pass_fds"])
def test_an_option_that_forces_a_fork_is_refused(option: str, tmp_path: Path) -> None:
    value = {
        "cwd": tmp_path,
        "start_new_session": True,
        "preexec_fn": lambda: None,
        "pass_fds": (0,),
    }[option]
    with pytest.raises(ValueError, match=option):
        child_process.run([sys.executable, "-c", "pass"], **{option: value})


def test_a_missing_program_still_raises_file_not_found(fork_refused: None) -> None:
    with pytest.raises(FileNotFoundError):
        child_process.run(["openadapt-no-such-program"], capture_output=True)
