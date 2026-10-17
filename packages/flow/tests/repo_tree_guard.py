"""Fail a test run that leaves TRACKED repository files modified.

A test that writes into the checkout is a hygiene defect, not a harmless
side effect:

* ``git status`` is dirty after a test run, so a developer can commit a
  regenerated golden by accident;
* ``scripts/check_release_consistency.py`` pins a reviewed SHA-256 inventory
  of the public files, so a mutated tracked file silently changes what that
  release gate measures;
* CI artifact and consistency jobs see a diff nobody wrote.

The guard snapshots the tracked-file status before the first test and again
after the last one, and fails the session when a NEW entry appeared. It
compares against a baseline instead of demanding a clean tree, so a developer
who runs the suite on top of their own uncommitted work is not punished for
it. Untracked files are out of scope (``-uno``): build output, caches and
scratch directories land there legitimately.

Set ``OPENADAPT_FLOW_ALLOW_DIRTY_TREE=1`` to disable the guard -- for example
when you edit files while a long suite runs, which would otherwise read as a
test-induced mutation.

:mod:`tests.conftest` binds the two entry points below -- :func:`capture_baseline`
and :func:`check_and_report` -- to the pytest session hooks. They take the repo
root as an argument, so a test can drive the same code against a throwaway git
repository.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

#: Environment variable that turns the guard off.
OPT_OUT_ENV = "OPENADAPT_FLOW_ALLOW_DIRTY_TREE"

#: Seconds allowed for one ``git status`` call. The guard is best-effort: a
#: slow or wedged git must not hang the whole test session.
GIT_TIMEOUT_S = 60


def guard_is_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Report whether the guard should run."""

    env = os.environ if environ is None else environ
    return env.get(OPT_OUT_ENV, "").strip().lower() not in {"1", "true", "yes", "on"}


def tracked_status(root: Path) -> tuple[str, ...] | None:
    """Return the porcelain status of TRACKED files under ``root``.

    Returns ``None`` -- meaning "cannot tell" -- when git is missing, when
    ``root`` is not a work tree, or when git fails or times out. The caller
    then skips the check rather than inventing a failure.
    """

    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return tuple(line for line in completed.stdout.splitlines() if line.strip())


def new_dirty_entries(
    before: tuple[str, ...] | None,
    after: tuple[str, ...] | None,
) -> tuple[str, ...]:
    """Return the status entries present in ``after`` but not in ``before``.

    A missing snapshot on either side yields no entries: an unknown baseline
    cannot prove that the run dirtied anything.
    """

    if before is None or after is None:
        return ()
    baseline = set(before)
    return tuple(entry for entry in after if entry not in baseline)


def format_failure(entries: tuple[str, ...], *, limit: int = 20) -> str:
    """Render an actionable message for the newly dirtied tracked files."""

    shown = entries[:limit]
    lines = [
        "Tracked repository files changed during the test run.",
        "",
        "A test must write to a tmp_path copy, never into the checkout.",
        f"{len(entries)} newly dirty tracked path(s):",
    ]
    lines.extend(f"  {entry}" for entry in shown)
    if len(entries) > len(shown):
        lines.append(f"  ... and {len(entries) - len(shown)} more")
    lines.extend(
        [
            "",
            "If a test wrote them, restore them with `git restore <path>` and",
            "give that test its own output directory. If you edited them",
            f"yourself while the suite ran, set {OPT_OUT_ENV}=1 to",
            "skip this check.",
        ]
    )
    return "\n".join(lines)


#: Where the baseline snapshot is stashed on the pytest config object.
BASELINE_ATTR = "_openadapt_flow_tracked_baseline"


def capture_baseline(config: pytest.Config, root: Path) -> None:
    """Record the tracked-file status of ``root`` on ``config``.

    Records nothing -- which disables the later check -- on an xdist worker
    (the controller runs the check once for the whole session) or when the
    opt-out environment variable is set.
    """

    if hasattr(config, "workerinput"):
        return
    if not guard_is_enabled():
        return
    setattr(config, BASELINE_ATTR, tracked_status(root))


def check_and_report(session: pytest.Session, exitstatus: int, root: Path) -> bool:
    """Report and fail the session if the run dirtied tracked files.

    Returns whether newly dirty entries were found. Turns a passing run into
    ``TESTS_FAILED``; a run that already failed keeps its own exit status.
    """

    baseline = getattr(session.config, BASELINE_ATTR, None)
    if baseline is None:
        return False
    entries = new_dirty_entries(baseline, tracked_status(root))
    if not entries:
        return False
    message = format_failure(entries)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        print(message)
    else:
        reporter.write_sep("=", "repository tree guard", red=True)
        reporter.write_line(message)
    if exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    return True
