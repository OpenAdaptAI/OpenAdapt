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


def test_intent_ignores_an_added_then_reverted_desktop_change(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    change = _commit(repo, "fix: temporary tray change", touch="src/tray.txt")
    _git(repo, "revert", "--no-edit", change)
    head = _git(repo, "rev-parse", "HEAD")
    assert prepare_release.commits_between(repo, base, head)
    assert prepare_release.commits_for_net_diff(repo, base, head) == []
    assert check_release_intent.check(repo, base, head) == []


def test_intent_retains_tests_after_reverting_runtime_without_a_bump(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    change = _commit(repo, "fix: temporary tray change", touch="src/tray.txt")
    _git(repo, "revert", "--no-edit", change)
    head = _commit(repo, "test: retain the tray contract", touch="tests/test_tray.py")
    assert check_release_intent.check(repo, base, head) == []


def test_intent_keeps_the_required_bump_for_a_remaining_runtime_fix(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    change = _commit(repo, "feat: canceled screen", touch="src/canceled.txt")
    _git(repo, "revert", "--no-edit", change)
    head = _commit(repo, "fix: surviving tray repair", touch="src/tray.txt")
    problems = check_release_intent.check(repo, base, head)
    assert len(problems) == 1
    assert "--bump patch" in problems[0]
    assert "surviving tray repair" in problems[0]
    assert "canceled screen" not in problems[0]


def test_canceled_feature_does_not_inflate_a_later_fix_on_the_same_path(repo: Path) -> None:
    _commit(repo, "chore: seed the tray", touch="src/tray.txt")
    base = _git(repo, "rev-parse", "HEAD")
    change = _commit(repo, "feat: canceled tray feature", touch="src/tray.txt")
    _git(repo, "revert", "--no-edit", change)
    head = _commit(repo, "fix: final tray repair", touch="src/tray.txt")
    commits = prepare_release.commits_for_net_diff(repo, base, head)
    assert [c.subject for c in commits] == ["fix: final tray repair"]
    assert prepare_release.required_level(commits, _version(repo)) == "patch"
    assert "--bump patch" in check_release_intent.check(repo, base, head)[0]


def test_surviving_feature_still_requires_minor_after_a_later_same_file_fix(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "feat: new tray feature", touch="src/tray.txt")
    head = _commit(repo, "fix: refine the tray feature", touch="src/tray.txt")
    assert "--bump minor" in check_release_intent.check(repo, base, head)[0]


@pytest.mark.parametrize("operation", ["rename", "delete", "mode"])
def test_net_path_operations_keep_release_intent(repo: Path, operation: str) -> None:
    _commit(repo, "chore: seed runtime", touch="src/runtime.txt")
    base = _git(repo, "rev-parse", "HEAD")
    if operation == "rename":
        _git(repo, "mv", "src/runtime.txt", "src/renamed.txt")
    elif operation == "delete":
        _git(repo, "rm", "src/runtime.txt")
    else:
        _git(repo, "update-index", "--chmod=+x", "src/runtime.txt")
    # _commit adds working-tree bytes, so retain the index-only mode directly.
    if operation == "mode":
        _git(repo, "commit", "-q", "-m", "fix: runtime mode")
        head = _git(repo, "rev-parse", "HEAD")
    else:
        head = _commit(repo, "fix: runtime path")
    assert "--bump patch" in check_release_intent.check(repo, base, head)[0]


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(
            "src/line\nbreak.txt",
            marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX filename control"),
        ),
        pytest.param(
            "src/tab\tname.txt",
            marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX filename control"),
        ),
        pytest.param(
            ":(glob)literal.txt",
            marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX filename control"),
        ),
        "unlisted/runtime.dat",
    ],
)
def test_net_paths_are_literal_nul_delimited_and_unknown_paths_ship(repo: Path, path: str) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "fix: exact runtime path", touch=path)
    commits = prepare_release.commits_for_net_diff(repo, base, head)
    assert commits[0].paths == (path,)
    assert "--bump patch" in check_release_intent.check(repo, base, head)[0]


def test_merge_resolution_only_runtime_change_refuses_classification(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    branch = _git(repo, "branch", "--show-current")
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "docs: side", touch="docs/side.md")
    _git(repo, "checkout", "-q", branch)
    _commit(repo, "docs: main", touch="docs/main.md")
    _git(repo, "merge", "--no-commit", "--no-ff", "side")
    head = _commit(repo, "merge: resolution adds runtime", touch="src/merge-only.txt")
    with pytest.raises(ValueError, match="no non-merge commit metadata"):
        check_release_intent.check(repo, base, head)


def test_net_diff_does_not_hide_version_consistency_problems(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    change = _commit(repo, "fix: canceled runtime", touch="src/tray.txt")
    _git(repo, "revert", "--no-edit", change)
    native = _load("native_release")
    native.set_native_version(prepare_release.bump(_version(repo), "patch"), repo)
    head = _commit(repo, "chore: change only native version")
    assert any(
        "native installer" in problem for problem in check_release_intent.check(repo, base, head)
    )


def test_net_diff_is_relative_to_desktop_inside_the_monorepo(repo: Path) -> None:
    package = repo / "packages" / "desktop"
    for relative in VERSION_FILES:
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(repo / relative, target)
    base = _commit(repo, "chore: nest Desktop in the monorepo")
    change = _commit(package, "feat: canceled Desktop feature", touch="src/tray.txt")
    _git(package, "revert", "--no-edit", change)
    head = _commit(repo, "feat: another package", touch="packages/other/runtime.txt")
    assert check_release_intent.check(package, base, head) == []
    head = _commit(package, "fix: Desktop tray repair", touch="src/tray.txt")
    problems = check_release_intent.check(package, base, head)
    assert len(problems) == 1 and "--bump patch" in problems[0]
    assert "another package" not in problems[0]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX filename control")
def test_crlf_filename_keeps_the_actual_net_release_intent(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "fix: literal CRLF path", touch="src/carriage\r\nline.txt")
    problems = check_release_intent.check(repo, base, head)
    assert len(problems) == 1 and "--bump patch" in problems[0]


def test_inconsistent_tree_metadata_cannot_erase_a_net_change(repo: Path, monkeypatch) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "fix: visible net change", touch="src/runtime.txt")
    monkeypatch.setattr(prepare_release, "_path_state", lambda *_args: None)
    with pytest.raises(ValueError, match="does not match exact tree metadata"):
        check_release_intent.check(repo, base, head)


@pytest.mark.parametrize("edited", [False, True])
def test_feature_intent_survives_a_later_rename(repo: Path, edited: bool) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "feat: new runtime component", touch="src/original.txt")
    _git(repo, "mv", "src/original.txt", "src/renamed.txt")
    if edited:
        (repo / "src/renamed.txt").write_text("refined runtime component\n")
    head = _commit(repo, "fix: move the runtime component")
    problems = check_release_intent.check(repo, base, head)
    assert len(problems) == 1 and "--bump minor" in problems[0]
    assert "feat: new runtime component" in problems[0]


def test_canceled_feature_does_not_inflate_a_later_rename(repo: Path) -> None:
    _commit(repo, "chore: seed runtime", touch="src/original.txt")
    base = _git(repo, "rev-parse", "HEAD")
    feature = _commit(repo, "feat: canceled runtime change", touch="src/original.txt")
    _git(repo, "revert", "--no-edit", feature)
    _git(repo, "mv", "src/original.txt", "src/renamed.txt")
    head = _commit(repo, "fix: move the original runtime")
    assert "--bump patch" in check_release_intent.check(repo, base, head)[0]


@pytest.mark.parametrize("final_name", ["src/third.txt", "src/original.txt"])
def test_feature_lineage_survives_multiple_moves(repo: Path, final_name: str) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "feat: new runtime component", touch="src/original.txt")
    _git(repo, "mv", "src/original.txt", "src/second.txt")
    _commit(repo, "fix: first runtime move")
    _git(repo, "mv", "src/second.txt", final_name)
    head = _commit(repo, "fix: second runtime move")
    assert "--bump minor" in check_release_intent.check(repo, base, head)[0]
