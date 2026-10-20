"""The monorepo release path: prepare_release.py and check_release_intent.py."""

from __future__ import annotations

import datetime
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
VERSION_FILES = (
    "pyproject.toml",
    "openadapt_desktop/__init__.py",
    "uv.lock",
    "package.json",
    "package-lock.json",
    "src-tauri/Cargo.toml",
    "src-tauri/Cargo.lock",
    "src-tauri/tauri.conf.json",
    "CHANGELOG.md",
)


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


prepare_release = _load("prepare_release")
check_release_intent = _load("check_release_intent")
check_release_consistency = _load("check_release_consistency")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(root: Path, message: str, touch: str | None = None) -> str:
    if touch is not None:
        path = root / touch
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{message}\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository holding Desktop's real version files."""

    for relative in VERSION_FILES:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _commit(tmp_path, "chore: start from the current release")
    return tmp_path


def _version(root: Path) -> str:
    return check_release_consistency.release_versions(root)["pyproject.toml"]


def test_bump_levels() -> None:
    assert prepare_release.bump("0.20.0", "patch") == "0.20.1"
    assert prepare_release.bump("0.20.3", "minor") == "0.21.0"
    assert prepare_release.bump("0.20.3", "major") == "1.0.0"


def test_required_level_follows_conventional_commits() -> None:
    commit = prepare_release.Commit
    assert prepare_release.required_level([commit("a", "docs: words", "")], "0.20.0") is None
    assert prepare_release.required_level([commit("a", "fix: x", "")], "0.20.0") == "patch"
    assert (
        prepare_release.required_level(
            [commit("a", "fix: x", ""), commit("b", "feat(ui): y", "")], "0.20.0"
        )
        == "minor"
    )
    # A breaking change on 0.x is a minor bump, as semantic-release did here.
    assert prepare_release.required_level([commit("a", "feat!: y", "")], "0.20.0") == "minor"
    assert prepare_release.required_level([commit("a", "feat!: y", "")], "1.2.0") == "major"


def test_prepare_sets_every_version_source_and_the_changelog(repo: Path) -> None:
    current = _version(repo)
    _commit(repo, "feat(portal): swipe to approve", touch="src/portal.txt")
    _commit(repo, "docs: explain the portal", touch="docs/portal.md")

    version = prepare_release.prepare(repo, today=datetime.date(2026, 10, 18))

    assert version == prepare_release.bump(current, "minor")
    versions = check_release_consistency.release_versions(repo)
    assert set(versions.values()) == {version}
    native = _load("native_release")
    assert native.native_version(repo) == version
    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert changelog.startswith(f"# CHANGELOG\n\n\n## v{version} (2026-10-18)\n")
    assert "- **portal**: swipe to approve" in changelog
    assert "explain the portal" not in changelog


def test_prepare_works_on_a_crlf_checkout(repo: Path) -> None:
    """Git on Windows checks files out with CRLF; every version source must still move."""

    for relative in VERSION_FILES:
        path = repo / relative
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    # On Windows the checkout (and so the fixture) is already CRLF, so the
    # rewrite can leave nothing to commit; the test is the prepare() below.
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "chore: check out with CRLF")
    _commit(repo, "fix(portal): keep the card", touch="src/portal.txt")

    version = prepare_release.prepare(repo, today=datetime.date(2026, 10, 19))

    assert set(check_release_consistency.release_versions(repo).values()) == {version}
    assert _load("native_release").native_version(repo) == version


def test_prepare_refuses_without_a_releasable_commit(repo: Path) -> None:
    _commit(repo, "docs: only words", touch="docs/words.md")
    with pytest.raises(ValueError, match="no feat, fix, perf"):
        prepare_release.prepare(repo)


def test_intent_passes_a_change_that_should_not_ship(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "test: cover the portal", touch="tests/test_x.py")
    assert check_release_intent.check(repo, base, head) == []


def test_intent_fails_an_unbumped_fix_and_names_the_command(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "fix: keep the tray alive", touch="src/tray.txt")
    problems = check_release_intent.check(repo, base, head)
    assert len(problems) == 1
    assert "fix: keep the tray alive" in problems[0]
    assert "prepare_release.py --bump patch" in problems[0]


def test_intent_passes_a_prepared_release(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "feat: a new screen", touch="src/screen.txt")
    prepare_release.prepare(repo, today=datetime.date(2026, 10, 18))
    head = _commit(repo, "chore: release")
    assert check_release_intent.check(repo, base, head) == []


def test_intent_fails_a_bump_smaller_than_the_commits_need(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "feat: a new screen", touch="src/screen.txt")
    prepare_release.prepare(repo, level="patch", today=datetime.date(2026, 10, 18))
    head = _commit(repo, "chore: release")
    problems = check_release_intent.check(repo, base, head)
    assert any("smaller than the minor bump" in problem for problem in problems)


def test_intent_fails_a_native_version_that_moves_alone(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    native = _load("native_release")
    native.set_native_version(prepare_release.bump(_version(repo), "patch"), repo)
    head = _commit(repo, "chore(native): move the installer version")
    problems = check_release_intent.check(repo, base, head)
    assert any("native installer" in problem for problem in problems)


def test_intent_fails_a_bump_without_its_changelog_section(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    version = prepare_release.bump(_version(repo), "patch")
    prepare_release.set_engine_version(repo, version)
    head = _commit(repo, "fix: something")
    problems = check_release_intent.check(repo, base, head)
    assert any(f"CHANGELOG.md must start with v{version}" in problem for problem in problems)


def test_intent_judges_the_merge_not_a_stale_branch_head(repo: Path) -> None:
    """A docs-only branch cut before a version bump must not be told to downgrade."""

    start = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "docs-branch")
    _commit(repo, "docs: explain the tray", touch="docs/tray.md")
    _git(repo, "checkout", "-q", "-")
    _commit(repo, "fix: keep the tray alive", touch="src/tray.txt")
    prepare_release.prepare(repo, today=datetime.date(2026, 10, 18))
    base = _commit(repo, "chore: release")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge docs", "docs-branch")
    merge = _git(repo, "rev-parse", "HEAD")
    assert start != base
    assert check_release_intent.check(repo, base, merge) == []


def test_a_fix_confined_to_tests_docs_or_ci_needs_no_release(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "fix(ci): tighten a release check", touch="tests/test_release.py")
    _commit(repo, "fix(docs): correct the runbook", touch="docs/RUNBOOK.md")
    head = _commit(repo, "feat: describe the portal", touch="README.md")
    assert check_release_intent.check(repo, base, head) == []
    # Anything outside those paths ships, including an unlisted one.
    head = _commit(repo, "fix: patch a build script", touch="scripts/build_frozen_engine.py")
    problems = check_release_intent.check(repo, base, head)
    assert len(problems) == 1
    assert "fix: patch a build script" in problems[0]
    assert "fix(ci): tighten a release check" not in problems[0]
