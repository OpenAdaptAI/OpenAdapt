from __future__ import annotations

import importlib.util
import re
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "plan_release.py"
SPEC = importlib.util.spec_from_file_location("plan_release", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
# `@dataclass` resolves annotations through `sys.modules[cls.__module__]`, so a
# module loaded straight from a path has to be registered before it executes.
sys.modules["plan_release"] = MODULE
SPEC.loader.exec_module(MODULE)

# The release workflow greps CHANGELOG.md with exactly this pattern, so a
# section this module renders has to match it or the publish refuses.
RELEASE_HEADING = re.compile(r"^## v[0-9]+\.[0-9]+\.[0-9]+ \([0-9]{4}-[0-9]{2}-[0-9]{2}\)$")


def _change(kind: str, *, scope: str = "", breaking: bool = False) -> object:
    return MODULE.Change(
        commit="a" * 40,
        type=kind,
        scope=scope,
        summary=f"{kind} something",
        breaking=breaking,
    )


@pytest.mark.parametrize(
    "current,kinds,breaking,major_on_zero,expected",
    [
        ("0.92.0", (), False, False, "0.92.0"),
        ("0.92.0", ("docs", "ci", "chore"), False, False, "0.92.0"),
        ("0.92.0", ("fix",), False, False, "0.92.1"),
        ("0.92.0", ("perf",), False, False, "0.92.1"),
        ("0.92.0", ("feat",), False, False, "0.93.0"),
        ("0.92.0", ("fix", "feat"), False, False, "0.93.0"),
        # major_on_zero is false in this repository, so a breaking change on a
        # 0.x line moves the minor, not the major.
        ("0.92.0", ("feat",), True, False, "0.93.0"),
        ("0.92.0", ("fix",), True, False, "0.93.0"),
        ("0.92.0", ("feat",), True, True, "1.0.0"),
        ("1.4.2", ("feat",), True, False, "2.0.0"),
        ("1.4.2", ("feat",), False, False, "1.5.0"),
        ("1.4.2", ("fix",), False, False, "1.4.3"),
    ],
)
def test_next_version_follows_the_declared_policy(
    current: str,
    kinds: tuple[str, ...],
    breaking: bool,
    major_on_zero: bool,
    expected: str,
) -> None:
    changes = tuple(_change(kind, breaking=breaking and index == 0) for index, kind in enumerate(kinds))
    assert MODULE.next_version(current, changes, major_on_zero=major_on_zero) == expected


def test_repository_policy_is_the_one_this_module_assumes() -> None:
    major_on_zero, allow_zero_version = MODULE._policy(ROOT)
    assert major_on_zero is False
    assert allow_zero_version is True


def test_rendered_section_matches_what_the_release_workflow_greps() -> None:
    release = MODULE.ReleasePlan(
        previous_version="0.92.0",
        next_version="0.93.0",
        changes=(_change("feat", scope="evidence"), _change("fix")),
    )
    section = MODULE.render_section(release, released_on=date(2026, 8, 26))
    first = section.splitlines()[0]

    assert RELEASE_HEADING.match(first)
    assert first == "## v0.93.0 (2026-08-26)"
    assert "### Features" in section
    assert "### Bug Fixes" in section
    assert section.index("### Features") < section.index("### Bug Fixes")
    assert "- **evidence**: Feat something" in section
    assert "/commit/" + "a" * 40 in section


def test_render_omits_sections_with_no_commits() -> None:
    release = MODULE.ReleasePlan(
        previous_version="0.92.0",
        next_version="0.92.1",
        changes=(_change("fix"),),
    )
    section = MODULE.render_section(release, released_on=date(2026, 8, 26))

    assert "### Bug Fixes" in section
    assert "### Features" not in section
    assert "### Chores" not in section


def _repository(tmp_path: Path, *, version: str, changelog: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "example"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    return tmp_path


def test_write_stamps_the_version_and_puts_the_section_on_top(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        version="0.92.0",
        changelog="# CHANGELOG\n\n\n## v0.92.0 (2026-08-22)\n\nolder text\n",
    )
    release = MODULE.ReleasePlan(
        previous_version="0.92.0",
        next_version="0.93.0",
        changes=(_change("feat"),),
    )

    MODULE.write(release, released_on=date(2026, 8, 26), root=root)

    metadata = (root / "pyproject.toml").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert 'version = "0.93.0"' in metadata
    assert 'version = "0.92.0"' not in metadata
    assert changelog.index("## v0.93.0 (2026-08-26)") < changelog.index("## v0.92.0 (2026-08-22)")
    assert changelog.startswith("# CHANGELOG")
    assert "older text" in changelog


def test_write_refuses_when_nothing_is_releasable(tmp_path: Path) -> None:
    root = _repository(tmp_path, version="0.92.0", changelog="# CHANGELOG\n")
    release = MODULE.ReleasePlan(
        previous_version="0.92.0",
        next_version="0.92.0",
        changes=(_change("docs"),),
    )

    with pytest.raises(MODULE.ReleasePlanError, match="no releasable change"):
        MODULE.write(release, released_on=date(2026, 8, 26), root=root)


def test_a_staged_release_is_a_quiet_no_op_not_a_failure() -> None:
    staged = MODULE.ReleasePlan(
        previous_version="0.93.0",
        next_version="0.93.0",
        changes=(),
        staged=True,
    )

    assert staged.released is False
    assert staged.staged is True
