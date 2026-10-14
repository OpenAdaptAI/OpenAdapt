"""Guard against absolute developer paths in tracked files.

This is a public repository. A path like ``/Users/<someone>/oa/src/...`` baked
into shipped code breaks that code for every other user, and it leaks the
directory layout of the machine it was written on.

The failure this catches is real: ``real_data_loader.py`` shipped a default
capture path under one developer's home directory, so ``openadapt-viewer
benchmark`` raised FileNotFoundError for everyone else.

Documentation counts too. A command a reader cannot run without first editing
out someone else's home directory is a broken command. Write ``/path/to/...``,
which is what the docs here already use in the places that got it right.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# Extensions that hold code, code-adjacent configuration, or documentation.
SCANNED_SUFFIXES = {
    ".py", ".sh", ".html", ".js", ".css", ".json", ".yml", ".yaml", ".toml", ".md",
}

# Absolute home-directory prefixes that must never appear in tracked code.
FORBIDDEN_PREFIXES = ("/Users/", "/home/")

# Empty on purpose. There is no tracked file that needs an exception, and adding
# one should take a comment saying why.
ALLOWLIST: set[str] = set()

# This file necessarily spells out the prefixes it bans, so it always matches
# itself. Skipping it by path keeps the check honest for every other file.
SELF = "tests/test_no_hardcoded_paths.py"


def _tracked_files() -> list[str]:
    """List tracked files whose extension puts them in scope for this check."""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        name
        for name in result.stdout.splitlines()
        if Path(name).suffix in SCANNED_SUFFIXES
    ]


def test_tracked_files_have_no_absolute_home_paths():
    """No tracked file may hardcode an absolute home-directory path."""
    offenders: list[str] = []

    for name in _tracked_files():
        if name in ALLOWLIST or name == SELF:
            continue
        path = REPO_ROOT / name
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(content.splitlines(), start=1):
            if any(prefix in line for prefix in FORBIDDEN_PREFIXES):
                offenders.append(f"{name}:{line_number}: {line.strip()[:120]}")

    assert not offenders, (
        "Tracked files hardcode absolute home-directory paths. In code, resolve "
        "the location from an environment variable with a repository-relative "
        "fallback. In documentation, write /path/to/...\n" + "\n".join(offenders)
    )


def test_allowlist_entries_still_exist():
    """The allowlist must not outlive the files it exempts.

    Without this, a renamed or deleted file leaves a stale exemption that
    silently widens the hole in the guard above.
    """
    missing = [
        name for name in ALLOWLIST | {SELF} if not (REPO_ROOT / name).exists()
    ]
    assert not missing, (
        f"ALLOWLIST names files that no longer exist: {missing}. "
        "Remove the stale entries."
    )
