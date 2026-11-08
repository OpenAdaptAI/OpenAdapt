"""The monorepo release path: prepare_release.py and check_release_intent.py."""

from __future__ import annotations

import datetime
import importlib.util
import json
import re
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


@pytest.mark.parametrize("copy_kind", ["fix", "chore"])
def test_split_copy_then_delete_keeps_feature_intent(
    repo: Path, monkeypatch, capsys, copy_kind: str
) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "feat: new component", touch="src/original.txt")
    (repo / "src/final.txt").write_bytes((repo / "src/original.txt").read_bytes())
    _commit(repo, f"{copy_kind}: stage component relocation")
    (repo / "src/original.txt").unlink()
    head = _commit(repo, "chore: remove old component name")
    problems = check_release_intent.check(repo, base, head)
    monkeypatch.setattr(check_release_intent, "ROOT", repo)
    code = check_release_intent.main(["--base", base, "--head", head])
    captured = capsys.readouterr()
    assert len(problems) == 1 and "--bump minor" in problems[0]
    assert code == 1 and "--bump minor" in captured.err


@pytest.mark.parametrize("copy_kind", ["fix", "chore"])
@pytest.mark.parametrize("modified", [False, True])
@pytest.mark.parametrize("shape", ["chain", "fanout", "reversion"])
def test_stateful_copy_histories_preserve_feature_bytes_at_every_prefix(
    repo: Path, copy_kind: str, modified: bool, shape: str
) -> None:
    """An independent content oracle checks real Git history prefixes.

    A synthetic feature is a unique payload token. Every copy/edit preserves
    that token; a deletion or actual Git revert can remove it. The expected
    release level depends on final payloads, not the collector's path or
    lineage representation. All runtime files start absent at the base.
    """
    base = _git(repo, "rev-parse", "HEAD")
    source = repo / "src"
    source.mkdir()
    token = b"UNIQUE_SYNTHETIC_FEATURE\n"
    (source / "original.txt").write_bytes(token)
    feature = _commit(repo, "feat: synthetic stateful feature")

    def assert_prefix() -> None:
        present = [p.read_bytes() for p in source.iterdir() if p.is_file()]
        expected = "minor" if any(token in data for data in present) else None
        assert expected is not None or not present
        head = _git(repo, "rev-parse", "HEAD")
        commits = prepare_release.commits_for_net_diff(repo, base, head)
        assert prepare_release.required_level(commits, _version(repo)) == expected
        problems = check_release_intent.check(repo, base, head)
        if expected is None:
            assert problems == []
        else:
            assert len(problems) == 1 and "--bump minor" in problems[0]

    def copy_file(old: str, new: str) -> str:
        data = (source / old).read_bytes()
        if modified:
            data += b"maintenance edit preserves the feature\n"
        (source / new).write_bytes(data)
        sha = _commit(repo, f"{copy_kind}: copy {old} to {new}")
        assert_prefix()
        return sha

    def drop(name: str) -> None:
        (source / name).unlink()
        _commit(repo, f"chore: remove {name}")
        assert_prefix()

    assert_prefix()
    copy_file("original.txt", "second.txt")
    if shape == "chain":
        drop("original.txt")
        copy_file("second.txt", "third.txt")
        drop("second.txt")
        copy_file("third.txt", "fourth.txt")
        drop("third.txt")
    elif shape == "fanout":
        copy_file("original.txt", "third.txt")
        copy_file("second.txt", "fourth.txt")
        drop("original.txt")
        drop("second.txt")
    else:
        _git(repo, "revert", "--no-edit", feature)
        assert_prefix()
        copied = copy_file("second.txt", "third.txt")
        _git(repo, "revert", "--no-edit", copied)
        assert_prefix()
        copy_file("second.txt", "fourth.txt")
        drop("second.txt")
    # Removing every surviving copy restores the exact base. Historical
    # feature intent must not demand a release once no feature remains.
    for path in sorted(source.iterdir()):
        if path.is_file():
            drop(path.name)


@pytest.mark.parametrize("modified", [False, True])
def test_copy_over_an_existing_destination_retains_the_source_feature(
    repo: Path, modified: bool
) -> None:
    _commit(repo, "chore: seed destination", touch="src/existing.txt")
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "feat: new source component", touch="src/original.txt")
    data = (repo / "src/original.txt").read_bytes()
    if modified:
        data += b"maintenance edit\n"
    (repo / "src/existing.txt").write_bytes(data)
    copied = _commit(repo, "fix: replace destination from component")
    (repo / "src/original.txt").unlink()
    head = _commit(repo, "chore: remove original source")
    assert "--bump minor" in check_release_intent.check(repo, base, head)[0]
    _git(repo, "revert", "--no-edit", copied)
    assert check_release_intent.check(repo, base, _git(repo, "rev-parse", "HEAD")) == []


def test_exact_copy_can_retain_an_earlier_deleted_source_version(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "feat: component staged for relocation", touch="src/original.txt")
    staged = (repo / "src/original.txt").read_bytes()
    (repo / "src/original.txt").unlink()
    _commit(repo, "chore: remove old component")
    assert check_release_intent.check(repo, base, _git(repo, "rev-parse", "HEAD")) == []
    (repo / "src/final.txt").write_bytes(staged)
    head = _commit(repo, "chore: install staged component")
    assert "--bump minor" in check_release_intent.check(repo, base, head)[0]


@pytest.mark.parametrize("copy_kind", ["fix", "chore"])
def test_documentation_origin_does_not_become_feature_release_intent(
    repo: Path, copy_kind: str
) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "feat: describe a component", touch="docs/component.txt")
    target = repo / "src/component.txt"
    target.parent.mkdir()
    target.write_bytes((repo / "docs/component.txt").read_bytes())
    head = _commit(repo, f"{copy_kind}: add the shipped component")
    commits = prepare_release.commits_for_net_diff(repo, base, head)
    assert prepare_release.required_level(commits, _version(repo)) == (
        "patch" if copy_kind == "fix" else None
    )
    problems = check_release_intent.check(repo, base, head)
    if copy_kind == "fix":
        assert len(problems) == 1 and "--bump patch" in problems[0]
        assert "feat: describe a component" not in problems[0]
    else:
        assert problems == []


@pytest.mark.parametrize("modified", [False, True])
def test_shipped_feature_lineage_survives_a_documentation_transit(
    repo: Path, modified: bool
) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "feat: actual runtime component", touch="src/original.txt")
    stage = repo / "docs/staged.txt"
    stage.parent.mkdir()
    stage.write_bytes((repo / "src/original.txt").read_bytes())
    _commit(repo, "chore: stage component in another directory")
    (repo / "src/original.txt").unlink()
    _commit(repo, "chore: remove original component name")
    if modified:
        stage.write_bytes(stage.read_bytes() + b"maintenance edit\n")
        _commit(repo, "fix: maintain staged component")
    (repo / "src/final.txt").write_bytes(stage.read_bytes())
    head = _commit(repo, "chore: install staged component")
    assert "--bump minor" in check_release_intent.check(repo, base, head)[0]


def _merge_independent_release(repo: Path, main: str, feature: str, number: int) -> str:
    """A real merge adopts main's independently changed package metadata."""
    import json

    _git(repo, "checkout", "-q", main)
    package = repo / "package.json"
    data = json.loads(package.read_text())
    data["lineage_fixture"] = f"main-{number}"
    package.write_text(json.dumps(data) + "\n")
    base = _commit(repo, f"chore: independently prepare main {number}")
    _git(repo, "checkout", "-q", feature)
    merged = subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", main],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert merged.returncode in {0, 1}, merged.stderr
    if merged.returncode:
        assert _git(repo, "diff", "--name-only", "--diff-filter=U") == "package.json"
        _git(repo, "checkout", "--theirs", "--", "package.json")
        _git(repo, "add", "package.json")
    _commit(repo, f"merge: adopt main release metadata {number}")
    return base


@pytest.mark.parametrize("subject,expected", [("feat", "minor"), ("fix", "patch"), ("chore", None)])
@pytest.mark.parametrize("main_updates", [1, 2])
@pytest.mark.parametrize("operation", ["keep", "copy-delete", "modified-copy-delete"])
def test_historical_copy_sources_use_their_own_shared_ancestor(
    repo: Path,
    subject: str,
    expected: str | None,
    main_updates: int,
    operation: str,
) -> None:
    """Pre-merge file versions are ambient history, not unexplained net edits."""
    import json

    main = _git(repo, "branch", "--show-current")
    feature = "feature-history"
    _git(repo, "checkout", "-q", "-b", feature)
    origin = _commit(repo, f"{subject}: introduce host", touch="src/original.txt")
    package = repo / "package.json"
    data = json.loads(package.read_text())
    data["lineage_fixture"] = "branch-release"
    package.write_text(json.dumps(data) + "\n")
    _commit(repo, "chore: prepare branch release")
    for number in range(1, main_updates + 1):
        base = _merge_independent_release(repo, main, feature, number)
    if operation != "keep":
        source = repo / "src/original.txt"
        copied = repo / "src/copied.txt"
        copied.write_bytes(
            source.read_bytes() + (b"modified copy\n" if operation.startswith("modified") else b"")
        )
        _commit(repo, "chore: copy host to its final path")
        source.unlink()
        _commit(repo, "chore: delete previous host location")
        final_path = "src/copied.txt"
    else:
        final_path = "src/original.txt"
    head = _git(repo, "rev-parse", "HEAD")
    commits = prepare_release.commits_for_net_diff(repo, base, head)
    assert prepare_release.required_level(commits, _version(repo)) == expected
    assert any(commit.sha == origin and final_path in commit.paths for commit in commits)
    assert not any("package.json" in commit.paths for commit in commits)


def test_a_merge_only_source_cannot_gain_intent_by_copying_after_a_base_merge(repo: Path) -> None:
    """Historical baselines cannot turn unclassified merge payload into a chore."""
    main = _git(repo, "branch", "--show-current")
    feature = "merge-only-source"
    _git(repo, "checkout", "-q", "-b", feature)
    _commit(repo, "docs: branch context", touch="docs/branch.md")
    _git(repo, "checkout", "-q", main)
    base = _commit(repo, "docs: main context", touch="docs/main.md")
    _git(repo, "checkout", "-q", feature)
    _git(repo, "merge", "--no-ff", "--no-commit", main)
    _commit(repo, "merge: introduces an unclassified source", touch="src/merge-only.txt")
    source = repo / "src/merge-only.txt"
    copied = repo / "src/copy.txt"
    copied.write_bytes(source.read_bytes())
    _commit(repo, "chore: copy unclassified payload")
    source.unlink()
    head = _commit(repo, "chore: remove original unclassified payload")
    with pytest.raises(ValueError, match="no non-merge commit metadata"):
        prepare_release.commits_for_net_diff(repo, base, head)


@pytest.mark.parametrize("metadata", ["", "a" * 40 + "\n" + "b" * 40 + "\n"])
def test_historical_lineage_requires_a_unique_common_ancestor(
    repo: Path, monkeypatch, metadata: str
) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "fix: runtime change", touch="src/runtime.txt")
    original = prepare_release._git

    def incomplete(root: Path, *args: str) -> str:
        if args[:2] == ("merge-base", "--all"):
            return metadata
        return original(root, *args)

    monkeypatch.setattr(prepare_release, "_git", incomplete)
    with pytest.raises(ValueError, match="unique merge base"):
        prepare_release.commits_for_net_diff(repo, base, head)


def _two_sided_history(repo: Path, subject: str, reverse: bool, *, conflict: bool = False):
    host = repo / "src/host.txt"
    host.parent.mkdir(exist_ok=True)
    baseline = [f"ambient line {index}\n" for index in range(50)]
    host.write_text("".join(baseline))
    _commit(repo, "chore: existing host component")
    main = _git(repo, "branch", "--show-current")
    _git(repo, "checkout", "-q", "-b", "two-sided-feature")
    branch_line, main_line = (40, 0) if reverse else (0, 40)
    branch = list(baseline)
    branch[branch_line] = "branch-owned behavior\n"
    host.write_text("".join(branch))
    origin = _commit(repo, f"{subject}: change host behavior")
    _git(repo, "checkout", "-q", main)
    ambient = list(baseline)
    ambient[branch_line if conflict else main_line] = "ambient main behavior\n"
    host.write_text("".join(ambient))
    base = _commit(repo, "feat: independently change main host")
    _git(repo, "checkout", "-q", "two-sided-feature")
    merged = subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", main], cwd=repo, capture_output=True, text=True
    )
    assert merged.returncode == int(conflict), merged.stderr
    if not conflict:
        text = host.read_text()
        assert "branch-owned behavior" in text and "ambient main behavior" in text
    return base, origin, host


def _relocate_after_merge(repo: Path, host: Path, operation: str) -> str:
    if operation == "keep":
        return host.relative_to(repo).as_posix()
    copied = host.with_name("relocated.txt")
    copied.write_bytes(
        host.read_bytes()
        + (b"maintenance copy edit\n" if operation == "modified-copy-delete" else b"")
    )
    _commit(repo, "chore: stage merged host relocation")
    host.unlink()
    _commit(repo, "chore: remove old merged host path")
    return copied.relative_to(repo).as_posix()


@pytest.mark.parametrize("subject,expected", [("feat", "minor"), ("fix", "patch"), ("chore", None)])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("later", [False, True])
@pytest.mark.parametrize("operation", ["keep", "copy-delete", "modified-copy-delete"])
def test_clean_two_sided_same_file_merges_preserve_only_branch_intent(
    repo: Path, subject: str, expected: str | None, reverse: bool, later: bool, operation: str
) -> None:
    base, origin, host = _two_sided_history(repo, subject, reverse)
    _commit(repo, "merge: combine independent host edits")
    if later:
        _commit(repo, "chore: subsequent branch context", touch="docs/after-merge.md")
    final_path = _relocate_after_merge(repo, host, operation)
    head = _git(repo, "rev-parse", "HEAD")
    commits = prepare_release.commits_for_net_diff(repo, base, head)
    assert prepare_release.required_level(commits, _version(repo)) == expected
    assert any(c.sha == origin and final_path in c.paths for c in commits)
    assert all(c.sha != base for c in commits)
    text = (repo / final_path).read_text()
    assert "branch-owned behavior" in text and "ambient main behavior" in text


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("operation", ["keep", "copy-delete", "modified-copy-delete"])
def test_manual_extra_merge_bytes_refuse_even_after_copy_or_a_later_edit(
    repo: Path, reverse: bool, operation: str
) -> None:
    base, _origin, host = _two_sided_history(repo, "feat", reverse)
    lines = host.read_text().splitlines(keepends=True)
    lines[20] = "unexplained resolution bytes\n"
    host.write_text("".join(lines))
    _commit(repo, "merge: adds unexplained resolution content")
    final_path = _relocate_after_merge(repo, host, operation)
    current = repo / final_path
    current.write_text(current.read_text() + "later ordinary maintenance\n")
    head = _commit(repo, "fix: maintain resolved host")
    with pytest.raises(ValueError, match="unexplained content"):
        prepare_release.commits_for_net_diff(repo, base, head)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("operation", ["keep", "copy-delete", "modified-copy-delete"])
def test_conflicted_same_hunk_manual_resolution_stays_unattributable(
    repo: Path, reverse: bool, operation: str
) -> None:
    base, _origin, host = _two_sided_history(repo, "feat", reverse, conflict=True)
    lines = [f"ambient line {index}\n" for index in range(50)]
    lines[40 if reverse else 0] = "manual conflict resolution\n"
    host.write_text("".join(lines))
    _commit(repo, "merge: manually resolve competing host changes")
    _relocate_after_merge(repo, host, operation)
    head = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="no clean combination"):
        prepare_release.commits_for_net_diff(repo, base, head)


@pytest.mark.parametrize("subject,expected", [("feat", "minor"), ("fix", "patch"), ("chore", None)])
def test_same_file_merge_keeps_a_prior_canceled_feature_canceled(
    repo: Path, subject: str, expected: str | None
) -> None:
    host = repo / "src/host.txt"
    host.parent.mkdir(exist_ok=True)
    baseline = [f"ambient line {index}\n" for index in range(50)]
    host.write_text("".join(baseline))
    _commit(repo, "chore: baseline host")
    main = _git(repo, "branch", "--show-current")
    _git(repo, "checkout", "-q", "-b", "canceled-feature")
    lines = list(baseline)
    lines[0] = "temporary feature\n"
    host.write_text("".join(lines))
    _commit(repo, "feat: temporary host feature")
    host.write_text("".join(baseline))
    _commit(repo, "chore: cancel temporary feature")
    lines[0] = baseline[0]
    lines[10] = "retained change\n"
    host.write_text("".join(lines))
    retained = _commit(repo, f"{subject}: retained host change")
    _git(repo, "checkout", "-q", main)
    ambient = list(baseline)
    ambient[40] = "ambient main update\n"
    host.write_text("".join(ambient))
    base = _commit(repo, "feat: ambient host update")
    _git(repo, "checkout", "-q", "canceled-feature")
    _git(repo, "merge", "--no-ff", "--no-commit", main)
    _commit(repo, "merge: combine host changes")
    commits = prepare_release.commits_for_net_diff(repo, base, _git(repo, "rev-parse", "HEAD"))
    assert prepare_release.required_level(commits, _version(repo)) == expected
    assert any(c.sha == retained for c in commits)


@pytest.mark.parametrize("subject,expected", [("fix", "patch"), ("chore", None)])
def test_exact_reversion_after_combination_discards_merged_feature_intent(
    repo: Path, subject: str, expected: str | None
) -> None:
    base, origin, host = _two_sided_history(repo, "feat", False)
    _commit(repo, "merge: combine independent host edits")
    host.write_text(_git(repo, "show", base + ":src/host.txt") + "\n")
    _commit(repo, "chore: restore the exact ambient host")
    head = _commit(repo, f"{subject}: unrelated retained change", touch="src/retained.txt")
    commits = prepare_release.commits_for_net_diff(repo, base, head)
    assert prepare_release.required_level(commits, _version(repo)) == expected
    assert all(c.sha != origin for c in commits)


@pytest.mark.parametrize("subject,expected", [("fix", "patch"), ("chore", None)])
def test_test_merge_keeps_a_canceled_feature_out_of_surviving_intent(
    repo: Path, subject: str, expected: str | None
) -> None:
    base, _origin, host = _two_sided_history(repo, "feat", False)
    _commit(repo, "merge: combine independent host edits")
    host.write_text(_git(repo, "show", base + ":src/host.txt") + "\n")
    _commit(repo, "chore: restore exact ambient host")
    _commit(repo, f"{subject}: retained change", touch="src/retained.txt")
    branch = _git(repo, "branch", "--show-current")
    _git(repo, "checkout", "-q", "-b", "test-merge-base", base)
    _git(repo, "merge", "--no-ff", "--no-commit", branch)
    head = _commit(repo, "merge: test branch on main")
    commits = prepare_release.commits_for_net_diff(repo, base, head)
    assert prepare_release.required_level(commits, _version(repo)) == expected
    assert all("src/host.txt" not in c.paths for c in commits)


@pytest.mark.parametrize("relative", ["docs/merge.txt", "tests/merge.py", "release-notes.md"])
def test_unshipped_manual_merge_content_does_not_erase_clean_runtime_intent(
    repo: Path, relative: str
) -> None:
    base, origin, _host = _two_sided_history(repo, "feat", False)
    _commit(repo, "merge: retain non-shipped resolution", touch=relative)
    head = _git(repo, "rev-parse", "HEAD")
    commits = prepare_release.commits_for_net_diff(repo, base, head)
    assert prepare_release.required_level(commits, _version(repo)) == "minor"
    assert any(c.sha == origin and "src/host.txt" in c.paths for c in commits)
    assert all(relative not in c.paths for c in commits)


@pytest.mark.parametrize("relative", ["docs/merge.txt", "tests/merge.py", "release-notes.md"])
def test_unexplained_unshipped_merge_source_still_refuses_when_copied_into_runtime(
    repo: Path, relative: str
) -> None:
    base, _origin, _host = _two_sided_history(repo, "feat", False)
    _commit(repo, "merge: retain non-shipped resolution", touch=relative)
    target = repo / "src/imported.txt"
    target.write_bytes((repo / relative).read_bytes())
    head = _commit(repo, "chore: import resolved source into runtime")
    with pytest.raises(ValueError, match="no non-merge commit metadata"):
        prepare_release.commits_for_net_diff(repo, base, head)


@pytest.mark.parametrize("relative", ["docs/merge.txt", "tests/merge.py", "release-notes.md"])
def test_deleted_merge_only_source_still_refuses_an_exact_later_runtime_copy(
    repo: Path, relative: str
) -> None:
    base, _origin, _host = _two_sided_history(repo, "feat", False)
    merge = _commit(repo, "merge: retain non-shipped resolution", touch=relative)
    raw = (repo / relative).read_bytes()
    assert len(_git(repo, "rev-list", "--parents", "--max-count=1", merge).split()) == 3
    (repo / relative).unlink()
    _commit(repo, "chore: remove the unresolved source")
    target = repo / "src/imported.txt"
    target.write_bytes(raw)
    head = _commit(repo, "chore: install the earlier source bytes")
    with pytest.raises(ValueError, match="no non-merge commit metadata"):
        prepare_release.commits_for_net_diff(repo, base, head)


@pytest.mark.parametrize("modified", [False, True])
def test_shipped_intent_survives_multiple_nonshipped_copy_hops(repo: Path, modified: bool) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    origin = _commit(repo, "feat: runtime source", touch="src/original.txt")
    previous = repo / "src/original.txt"
    for relative in ("docs/first.txt", "tests/staged.py", "release-stage.md"):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(previous.read_bytes() + (b"copy maintenance\n" if modified else b""))
        _commit(repo, "chore: stage component data")
        previous.unlink()
        _commit(repo, "chore: remove previous component name")
        previous = target
    final = repo / "src/final.txt"
    final.write_bytes(previous.read_bytes())
    head = _commit(repo, "chore: ship the staged component")
    commits = prepare_release.commits_for_net_diff(repo, base, head)
    assert prepare_release.required_level(commits, _version(repo)) == "minor"
    assert any(c.sha == origin and "src/final.txt" in c.paths for c in commits)


@pytest.mark.parametrize("relative", ["docs/merge.txt", "tests/merge.py", "release-notes.md"])
@pytest.mark.parametrize("modification", ["append", "prefix", "replace-line"])
def test_modified_runtime_copy_of_unexplained_nonshipped_merge_still_refuses(
    repo: Path, relative: str, modification: str
) -> None:
    base, _origin, _host = _two_sided_history(repo, "feat", False)
    source = repo / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    raw = b"unattributed payload first\nunattributed payload second\nunattributed payload last\n"
    source.write_bytes(raw)
    merge = _commit(repo, "merge: introduce unexplained source data")
    assert len(_git(repo, "rev-list", "--parents", "--max-count=1", merge).split()) == 3
    changed = {
        "append": raw + b"ordinary maintenance\n",
        "prefix": b"ordinary maintenance\n" + raw,
        "replace-line": raw.replace(b"payload last", b"revised ending"),
    }[modification]
    assert changed != raw
    target = repo / "src/imported.txt"
    target.write_bytes(changed)
    head = _commit(repo, "fix: install modified source")
    with pytest.raises(ValueError, match="no non-merge commit metadata"):
        prepare_release.commits_for_net_diff(repo, base, head)


@pytest.mark.parametrize("relative", ["docs/shared.txt", "tests/shared.py", "shared-notes.md"])
@pytest.mark.parametrize("shared", ["ordinary shared scaffolding\n", "---------\n"])
def test_shared_ambient_content_is_not_an_unexplained_merge_source_edge(
    repo: Path, relative: str, shared: str
) -> None:
    data = repo / relative
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_text(shared + "original reference content\n")
    host = repo / "src/host.txt"
    host.parent.mkdir(exist_ok=True)
    baseline = [shared] + [f"existing host line {i}\n" for i in range(50)]
    host.write_text("".join(baseline))
    _commit(repo, "chore: baseline runtime and reference material")
    main = _git(repo, "branch", "--show-current")
    _git(repo, "checkout", "-q", "-b", "ambient-source-feature")
    branch = list(baseline)
    branch[5] = "branch runtime feature\n"
    host.write_text("".join(branch))
    origin = _commit(repo, "feat: runtime behavior")
    _git(repo, "checkout", "-q", main)
    ambient = list(baseline)
    ambient[45] = "main runtime behavior\n"
    host.write_text("".join(ambient))
    base = _commit(repo, "feat: independent main behavior")
    _git(repo, "checkout", "-q", "ambient-source-feature")
    _git(repo, "merge", "--no-ff", "--no-commit", main)
    # Shared text already exists in both parent files. Only the reference
    # file gains unexplained merge-only bytes; they do not enter the host.
    data.write_text(data.read_text() + "unexplained reference merge bytes\n")
    head = _commit(repo, "merge: retain unrelated reference resolution")
    commits = prepare_release.commits_for_net_diff(repo, base, head)
    assert prepare_release.required_level(commits, _version(repo)) == "minor"
    assert any(c.sha == origin and "src/host.txt" in c.paths for c in commits)


@pytest.mark.parametrize("relative", ["docs/merge.txt", "tests/merge.py", "release-notes.md"])
def test_completely_rewritten_move_of_unexplained_merge_source_still_refuses(
    repo: Path, relative: str
) -> None:
    base, _origin, _host = _two_sided_history(repo, "feat", False)
    source = repo / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("unexplained original content\n")
    _commit(repo, "merge: create unexplained source")
    source.unlink()
    (repo / "src/replacement.txt").write_text("entirely rewritten destination\n")
    head = _commit(repo, "fix: move and rewrite source")
    with pytest.raises(ValueError, match="no non-merge commit metadata"):
        prepare_release.commits_for_net_diff(repo, base, head)


def _edit_existing_component(lines: list[str], edit: str, position: int) -> list[str]:
    result = list(lines)
    if edit == "remove":
        del result[position]
    elif edit == "add":
        result.insert(position, "new component behavior\n")
    else:
        assert edit == "replace"
        result[position] = "replacement component behavior\n"
    return result


def _decorate_component(lines: list[str], where: str) -> list[str]:
    result = list(lines)
    position = {"prefix": 0, "middle": len(result) // 2, "suffix": len(result)}[where]
    result.insert(position, "copy maintenance annotation\n")
    return result


@pytest.mark.parametrize("edit", ["add", "remove", "replace"])
@pytest.mark.parametrize("position", [0, 4, 8])
@pytest.mark.parametrize("decoration", ["prefix", "middle", "suffix"])
@pytest.mark.parametrize("existing_destination", [False, True])
def test_source_edit_lineage_survives_a_modified_copy_and_source_restore(
    repo: Path, edit: str, position: int, decoration: str, existing_destination: bool
) -> None:
    original = [f"component behavior {n}\n" for n in range(9)]
    source, destination = repo / "src/component.txt", repo / "src/copied-component.txt"
    source.parent.mkdir(exist_ok=True)
    source.write_text("".join(original))
    if existing_destination:
        destination.write_text("".join(original))
    base = _commit(repo, "chore: existing component baseline")
    changed = _edit_existing_component(original, edit, position)
    source.write_text("".join(changed))
    feature = _commit(repo, "feat: update existing component behavior")
    destination.write_text("".join(_decorate_component(changed, decoration)))
    _commit(repo, "chore: maintain a component copy")
    source.write_text("".join(original))
    head = _commit(repo, "chore: restore original component")
    commits = prepare_release.commits_for_net_diff(repo, base, head)
    assert prepare_release.required_level(commits, _version(repo)) == "minor"
    assert any(c.sha == feature and "src/copied-component.txt" in c.paths for c in commits)
    problems = check_release_intent.check(repo, base, head)
    assert len(problems) == 1
    assert "--bump minor" in problems[0]


@pytest.mark.parametrize("finish", ["keep", "remove-later", "move"])
@pytest.mark.parametrize("position", [0, 8])
@pytest.mark.parametrize("decoration", ["prefix", "suffix"])
def test_removal_intent_reaches_destination_for_copy_and_move_lifetimes(
    repo: Path, finish: str, position: int, decoration: str
) -> None:
    original = [f"lifetime component {n}\n" for n in range(9)]
    source, destination = repo / "src/lifetime.txt", repo / "src/lifetime-copy.txt"
    source.parent.mkdir(exist_ok=True)
    source.write_text("".join(original))
    base = _commit(repo, "chore: baseline lifetime component")
    changed = _edit_existing_component(original, "remove", position)
    source.write_text("".join(changed))
    feature = _commit(repo, "feat: remove obsolete lifetime behavior")
    destination.write_text("".join(_decorate_component(changed, decoration)))
    if finish == "move":
        source.unlink()
    _commit(repo, "chore: relocate component with maintenance")
    if finish == "remove-later":
        source.unlink()
        _commit(repo, "chore: retire old component location")
    head = _git(repo, "rev-parse", "HEAD")
    commits = prepare_release.commits_for_net_diff(repo, base, head)
    assert prepare_release.required_level(commits, _version(repo)) == "minor"
    assert any(c.sha == feature and "src/lifetime-copy.txt" in c.paths for c in commits)


@pytest.mark.parametrize("relative", ["docs/transit.txt", "tests/transit.py"])
@pytest.mark.parametrize("decoration", ["prefix", "suffix"])
def test_removed_behavior_survives_modified_nonshipped_transit(
    repo: Path, relative: str, decoration: str
) -> None:
    original = [f"transit component {n}\n" for n in range(9)]
    source = repo / "src/transit-source.txt"
    source.parent.mkdir(exist_ok=True)
    source.write_text("".join(original))
    base = _commit(repo, "chore: baseline transit component")
    changed = _edit_existing_component(original, "remove", 4)
    source.write_text("".join(changed))
    feature = _commit(repo, "feat: remove obsolete transit behavior")
    transit = repo / relative
    transit.parent.mkdir(parents=True, exist_ok=True)
    transit.write_text("".join(_decorate_component(changed, decoration)))
    _commit(repo, "chore: retain an intermediate component copy")
    destination = repo / "src/transit-destination.txt"
    destination.write_bytes(transit.read_bytes() + b"second copy maintenance\n")
    _commit(repo, "chore: install intermediate component")
    source.write_text("".join(original))
    transit.unlink()
    head = _commit(repo, "chore: restore source and remove intermediate")
    commits = prepare_release.commits_for_net_diff(repo, base, head)
    assert prepare_release.required_level(commits, _version(repo)) == "minor"
    assert any(c.sha == feature and "src/transit-destination.txt" in c.paths for c in commits)


@pytest.mark.parametrize("edit", ["add", "remove", "replace"])
def test_canceled_source_edit_does_not_promote_a_later_maintenance_copy(
    repo: Path, edit: str
) -> None:
    original = [f"canceled component {n}\n" for n in range(9)]
    source = repo / "src/canceled-source.txt"
    source.parent.mkdir(exist_ok=True)
    source.write_text("".join(original))
    base = _commit(repo, "chore: canceled component baseline")
    source.write_text("".join(_edit_existing_component(original, edit, 4)))
    feature = _commit(repo, "feat: temporary component edit")
    source.write_text("".join(original))
    _commit(repo, "chore: cancel component edit")
    (repo / "src/canceled-copy.txt").write_text("".join(_decorate_component(original, "middle")))
    head = _commit(repo, "chore: retain only baseline component copy")
    commits = prepare_release.commits_for_net_diff(repo, base, head)
    assert prepare_release.required_level(commits, _version(repo)) is None
    assert all(c.sha != feature for c in commits)
    assert check_release_intent.check(repo, base, head) == []


@pytest.mark.parametrize("position", [0, 4, 8])
def test_a_copy_that_retains_removed_behavior_has_no_negative_edit_witness(
    repo: Path, position: int
) -> None:
    original = [f"retained component {n}\n" for n in range(9)]
    source = repo / "src/retained-source.txt"
    source.parent.mkdir(exist_ok=True)
    source.write_text("".join(original))
    base = _commit(repo, "chore: baseline retained behavior")
    source.write_text("".join(_edit_existing_component(original, "remove", position)))
    feature = _commit(repo, "feat: remove behavior in original location")
    (repo / "src/retained-copy.txt").write_text("".join(_decorate_component(original, "middle")))
    _commit(repo, "chore: copy baseline with behavior still present")
    source.write_text("".join(original))
    head = _commit(repo, "chore: restore baseline original")
    commits = prepare_release.commits_for_net_diff(repo, base, head)
    assert prepare_release.required_level(commits, _version(repo)) is None
    assert all(c.sha != feature for c in commits)


@pytest.mark.parametrize("relative", ["docs/removed.txt", "tests/removed.py"])
@pytest.mark.parametrize("decoration", ["prefix", "suffix"])
def test_modified_copy_of_unexplained_removal_source_still_refuses(
    repo: Path, relative: str, decoration: str
) -> None:
    source = repo / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    original = [f"unexplained removal component {n}\n" for n in range(9)]
    source.write_text("".join(original))
    _commit(repo, "chore: reference component baseline")
    base, _origin, _host = _two_sided_history(repo, "feat", False)
    source.write_text("".join(_edit_existing_component(original, "remove", 4)))
    _commit(repo, "merge: unexplained removal from reference component")
    (repo / "src/unexplained-removal-copy.txt").write_text(
        "".join(_decorate_component(source.read_text().splitlines(keepends=True), decoration))
    )
    head = _commit(repo, "chore: install modified reference copy")
    with pytest.raises(ValueError, match="no non-merge commit metadata"):
        prepare_release.commits_for_net_diff(repo, base, head)


@pytest.mark.parametrize("position", [0, 4, 8])
@pytest.mark.parametrize("decoration", ["prefix", "suffix"])
@pytest.mark.parametrize("existing_destination", [False, True])
def test_removing_one_duplicate_behavior_keeps_counted_copy_lineage(
    repo: Path, position: int, decoration: str, existing_destination: bool
) -> None:
    original = [f"duplicate component {n}\n" for n in range(9)]
    original[1] = original[position]
    source, destination = repo / "src/duplicate-source.txt", repo / "src/duplicate-copy.txt"
    source.parent.mkdir(exist_ok=True)
    source.write_text("".join(original))
    if existing_destination:
        destination.write_text("".join(original))
    base = _commit(repo, "chore: repeated behavior baseline")
    changed = _edit_existing_component(original, "remove", position)
    source.write_text("".join(changed))
    feature = _commit(repo, "feat: remove one duplicate behavior")
    destination.write_text("".join(_decorate_component(changed, decoration)))
    _commit(repo, "chore: retain repeated-behavior component copy")
    source.write_text("".join(original))
    head = _commit(repo, "chore: restore original repeated behavior")
    commits = prepare_release.commits_for_net_diff(repo, base, head)
    assert prepare_release.required_level(commits, _version(repo)) == "minor"
    assert any(c.sha == feature and "src/duplicate-copy.txt" in c.paths for c in commits)
    assert len(check_release_intent.check(repo, base, head)) == 1


# One field-ownership model for every output of set_engine_version. The
# placeholders are engine fields; equal dependency/comment/string versions
# deliberately remain ordinary bytes in the expected result.
_RELEASE_DOCUMENTS = {
    "pyproject.toml": (
        '[project]\nname = "openadapt-desktop"\nversion = "@ENGINE@"\n'
        '[tool.plugin]\nversion = "0.28.1"\n'
    ),
    "uv.lock": (
        'version = 1\n[[package]]\nname = "dependency"\nversion = "0.28.1"\n'
        '[[package]]\nname = "openadapt-desktop"\nversion = "@ENGINE@"\n'
        'source = { editable = "." }\n'
    ),
    "package.json": (
        '{"name":"openadapt-desktop", "version":"@ENGINE@", '
        '"plugin":{"version":"0.28.1"}, "note":"0.28.1"}\n'
    ),
    "package-lock.json": (
        '{"name":"openadapt-desktop", "version":"@ENGINE@", "packages":{'
        '"":{"name":"openadapt-desktop", "version":"@ENGINE@"}, '
        '"node_modules/plugin":{"version":"0.28.1"}}}\n'
    ),
    "src-tauri/tauri.conf.json": (
        '{"identifier":"synthetic.desktop", "version":"@ENGINE@", '
        '"plugins":{"plugin":{"version":"0.28.1"}}}\n'
    ),
    "src-tauri/Cargo.toml": (
        '[package]\nname = "openadapt-desktop"\nversion = "@ENGINE@"\n'
        '[dependencies]\nplugin = { version = "0.28.1" }\n'
    ),
    "src-tauri/Cargo.lock": (
        'version = 4\n[[package]]\nname = "plugin"\nversion = "0.28.1"\n'
        '[[package]]\nname = "openadapt-desktop"\nversion = "@ENGINE@"\n'
    ),
    "openadapt_desktop/__init__.py": (
        '"""Ambient example 0.28.1 and Unicode café."""\n'
        'OTHER_VERSION = "0.28.1"\n__version__ = "@ENGINE@"\n'
        '# __version__ = "0.28.1"\n'
    ),
}


@pytest.mark.parametrize("relative", _RELEASE_DOCUMENTS)
@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_release_literal_model_preserves_every_unowned_byte(relative: str, ending: str) -> None:
    template = _RELEASE_DOCUMENTS[relative].replace("\n", ending)
    original = template.replace("@ENGINE@", "0.28.1").encode("utf-8")
    expected = template.replace("@ENGINE@", "1.2.3").encode("utf-8")
    assert prepare_release._release_content(relative, original, "1.2.3") == expected


@pytest.mark.parametrize("relative", _RELEASE_DOCUMENTS)
@pytest.mark.parametrize("version", ["01.2.3", "1.2", "1.2.3rc1"])
def test_release_literal_model_refuses_malformed_engine_versions(
    relative: str, version: str
) -> None:
    original = _RELEASE_DOCUMENTS[relative].replace("@ENGINE@", version).encode("utf-8")
    with pytest.raises(ValueError):
        prepare_release._release_content(relative, original, "1.2.3")


@pytest.mark.parametrize("relative", _RELEASE_DOCUMENTS)
def test_release_literal_model_refuses_missing_fields(relative: str) -> None:
    text = _RELEASE_DOCUMENTS[relative]
    if relative.endswith(".json"):
        text = text.replace('"@ENGINE@"', "null")
    elif relative.endswith(".py"):
        text = text.replace('__version__ = "@ENGINE@"', 'UNRELATED = "0.28.1"')
    else:
        text = text.replace('version = "@ENGINE@"', 'missing = "0.28.1"')
    with pytest.raises(ValueError):
        prepare_release._release_content(relative, text.encode(), "1.2.3")


@pytest.mark.parametrize("relative", _RELEASE_DOCUMENTS)
def test_release_literal_model_refuses_duplicate_fields_or_self_packages(relative: str) -> None:
    text = _RELEASE_DOCUMENTS[relative].replace("@ENGINE@", "0.28.1")
    if relative.endswith(".json"):
        text = text.replace("{", '{"version":"0.28.1",', 1)
    elif relative in {"uv.lock", "src-tauri/Cargo.lock"}:
        text += '\n[[package]]\nname = "openadapt-desktop"\nversion = "0.28.1"\n'
        if relative == "uv.lock":
            text += 'source = { editable = "." }\n'
    elif relative.endswith(".py"):
        text += '__version__ = "0.28.1"\n'
    else:
        text = text.replace('version = "0.28.1"', 'version = "0.28.1"\nversion = "0.28.1"', 1)
    with pytest.raises(ValueError):
        prepare_release._release_content(relative, text.encode(), "1.2.3")


@pytest.mark.parametrize(
    "relative",
    [
        "pyproject.toml",
        "uv.lock",
        "package.json",
        "package-lock.json",
        "src-tauri/Cargo.toml",
        "src-tauri/Cargo.lock",
    ],
)
def test_release_literal_model_refuses_foreign_package_identity(relative: str) -> None:
    text = _RELEASE_DOCUMENTS[relative].replace("@ENGINE@", "0.28.1")
    text = text.replace("openadapt-desktop", "different-package")
    with pytest.raises(ValueError):
        prepare_release._release_content(relative, text.encode(), "1.2.3")


@pytest.mark.parametrize("shape", ["foreign-source", "inconsistent-lock", "non-json-constant"])
def test_release_literal_model_refuses_ambiguous_document_roles(shape: str) -> None:
    relative = "uv.lock" if shape == "foreign-source" else "package-lock.json"
    text = _RELEASE_DOCUMENTS[relative].replace("@ENGINE@", "0.28.1")
    if shape == "foreign-source":
        text = text.replace('editable = "."', 'registry = "https://example.invalid"')
    elif shape == "inconsistent-lock":
        text = text.replace('"version":"0.28.1"', '"version":"1.0.0"', 1)
    else:
        text = text.replace("{", '{"unrelated":NaN,', 1)
    with pytest.raises(ValueError):
        prepare_release._release_content(relative, text.encode(), "1.2.3")


def _version_merge_branches(repo: Path, metadata_edit: bool = False) -> tuple[str, str, str]:
    start = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "release-feature")
    if metadata_edit:
        source = repo / "pyproject.toml"
        source.write_text(
            source.read_text().replace("The OpenAdapt desktop app", "The synthetic desktop app")
        )
    _commit(repo, "feat: preserve the new feature", touch="src/feature.txt")
    prepare_release.prepare(repo, today=datetime.date(2026, 11, 1))
    feature = _commit(repo, "chore: prepare feature release")
    _git(repo, "checkout", "-q", "-b", "release-main", start)
    if metadata_edit:
        source = repo / "pyproject.toml"
        source.write_text(source.read_text() + "\n[tool.synthetic]\nambient = true\n")
    _commit(repo, "fix: independent ambient fix", touch="src/ambient.txt")
    prepare_release.prepare(repo, today=datetime.date(2026, 11, 2))
    main = _commit(repo, "chore: prepare ambient release")
    return start, feature, main


def _resolve_version_merge(
    repo: Path, feature: str, main: str, *, first: str, version: str | None = None
) -> str:
    _git(
        repo, "checkout", "-q", "-B", "release-resolution", feature if first == "feature" else main
    )
    other = main if first == "feature" else feature
    result = subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", other],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode in {0, 1}, result.stderr
    # Resolve only the generated version files. The independent runtime files
    # are the ordinary merge result; no source code is fabricated by the helper.
    for relative in VERSION_FILES:
        text = subprocess.run(
            ["git", "show", f"{main}:./{relative}"],
            cwd=repo,
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8")
        if relative == "pyproject.toml":
            feature_text = _git(repo, "show", f"{feature}:./{relative}")
            if "The synthetic desktop app" in feature_text:
                text = text.replace("The OpenAdapt desktop app", "The synthetic desktop app")
        (repo / relative).write_bytes(text.encode("utf-8"))
    selected = version or check_release_intent.version_at(repo, feature)
    prepare_release.set_engine_version(repo, selected)
    prepare_release.write_changelog(
        repo,
        prepare_release.changelog_section(
            selected,
            [
                prepare_release.Commit(
                    feature, "feat: preserve the new feature", "", ("src/feature.txt",)
                )
            ],
            datetime.date(2026, 11, 1),
        ),
    )
    return _commit(repo, "merge: combine generated release metadata")


@pytest.mark.parametrize("first", ["feature", "main"])
@pytest.mark.parametrize("metadata_edit", [False, True])
def test_generated_version_resolution_keeps_feature_intent_and_ambient_bytes(
    repo: Path, first: str, metadata_edit: bool
) -> None:
    _start, feature, main = _version_merge_branches(repo, metadata_edit)
    head = _resolve_version_merge(repo, feature, main, first=first)
    commits = prepare_release.commits_for_net_diff(repo, main, head)
    assert (
        prepare_release.required_level(commits, check_release_intent.version_at(repo, main))
        == "minor"
    )
    assert check_release_intent.check(repo, main, head) == []
    assert (repo / "src/feature.txt").read_text() == "feat: preserve the new feature\n"
    assert (repo / "src/ambient.txt").read_text() == "fix: independent ambient fix\n"
    notes = (repo / "CHANGELOG.md").read_text()
    assert "preserve the new feature" in notes
    assert "independent ambient fix" in notes
    if metadata_edit:
        metadata = (repo / "pyproject.toml").read_text()
        assert "The synthetic desktop app" in metadata
        assert "ambient = true" in metadata


@pytest.mark.parametrize("first", ["feature", "main"])
def test_repeated_generated_version_merges_keep_surviving_feature_intent(
    repo: Path, first: str
) -> None:
    _start, feature, main = _version_merge_branches(repo)
    merged = _resolve_version_merge(repo, feature, main, first=first)
    _git(repo, "checkout", "-q", "release-main")
    _commit(repo, "fix: another ambient correction", touch="src/ambient-second.txt")
    prepare_release.prepare(repo, today=datetime.date(2026, 11, 3))
    next_main = _commit(repo, "chore: prepare next ambient release")
    head = _resolve_version_merge(repo, merged, next_main, first=first)
    assert (
        prepare_release.required_level(
            prepare_release.commits_for_net_diff(repo, next_main, head),
            check_release_intent.version_at(repo, next_main),
        )
        == "minor"
    )
    assert check_release_intent.check(repo, next_main, head) == []


@pytest.mark.parametrize("shape", ["too-small", "inconsistent", "missing-changelog"])
def test_generated_version_resolution_keeps_all_final_release_guards(
    repo: Path, shape: str
) -> None:
    _start, feature, main = _version_merge_branches(repo)
    version = check_release_intent.version_at(repo, main) if shape == "too-small" else None
    _resolve_version_merge(repo, feature, main, first="feature", version=version)
    if shape == "inconsistent":
        source = repo / "openadapt_desktop/__init__.py"
        source.write_text(source.read_text().replace(_version(repo), "99.0.0"))
    elif shape == "missing-changelog":
        (repo / "CHANGELOG.md").write_text("# CHANGELOG\n\nNo dated release section.\n")
    head = _commit(repo, "chore: retain proposed resolution", touch="docs/check.txt")
    problems = check_release_intent.check(repo, main, head)
    assert problems
    expected = {
        "too-small": "should ship",
        "inconsistent": "version sources disagree",
        "missing-changelog": "CHANGELOG.md must start",
    }
    assert any(expected[shape] in problem for problem in problems)


@pytest.mark.parametrize("relative", [path for path in VERSION_FILES if path != "CHANGELOG.md"])
def test_generated_version_resolution_cannot_hide_unexplained_nonversion_bytes(
    repo: Path, relative: str
) -> None:
    _start, feature, main = _version_merge_branches(repo)
    _resolve_version_merge(repo, feature, main, first="feature")
    # Keep the edit in the merge itself rather than an ordinary attributed
    # commit. The resulting exact tree must be rejected, even on owned files.
    source = repo / relative
    text = source.read_text()
    if relative.endswith(".json"):
        text = text.replace("{", '{"unexplained_plugin_version":"77.0.0",', 1)
    elif relative.endswith(".py"):
        text += '\nUNEXPLAINED_RUNTIME_VALUE = "77.0.0"\n'
    else:
        text += '\n[unexplained]\nplugin_version = "77.0.0"\n'
    source.write_text(text)
    _git(repo, "add", "--", relative)
    tree = _git(repo, "write-tree")
    head = _git(
        repo,
        "commit-tree",
        tree,
        "-p",
        feature,
        "-p",
        main,
        "-m",
        "merge: unexplained nonversion edit",
    )
    with pytest.raises(
        ValueError, match="unexplained content|no clean combination|no commit intent"
    ):
        prepare_release.commits_for_net_diff(repo, main, head)


@pytest.mark.parametrize("first", ["feature", "main"])
def test_generated_version_selection_may_exceed_an_unchanged_parent_version(
    repo: Path, first: str
) -> None:
    start, feature, _released_main = _version_merge_branches(repo)
    _git(repo, "checkout", "-q", "-b", "unreleased-main", start)
    main = _commit(
        repo, "fix: ambient change without preparing a release", touch="src/unreleased.txt"
    )
    version = prepare_release.bump(check_release_intent.version_at(repo, feature), "minor")
    head = _resolve_version_merge(repo, feature, main, first=first, version=version)
    assert check_release_intent.check(repo, main, head) == []
    assert (
        prepare_release.required_level(
            prepare_release.commits_for_net_diff(repo, main, head),
            check_release_intent.version_at(repo, main),
        )
        == "minor"
    )


@pytest.mark.parametrize(
    "relative",
    [
        "package.json",
        "package-lock.json",
        "uv.lock",
        "src-tauri/Cargo.toml",
        "src-tauri/Cargo.lock",
    ],
)
def test_release_normalization_never_absorbs_a_dependency_version_edit(
    repo: Path, relative: str
) -> None:
    _start, feature, main = _version_merge_branches(repo)
    _resolve_version_merge(repo, feature, main, first="main")
    source = repo / relative
    text = source.read_text()
    if relative.endswith(".json"):
        document = json.loads(text)
        if relative == "package.json":
            dependency = next(iter(document["dependencies"]))
            document["dependencies"][dependency] = "99.0.0"
        else:
            dependency = next(key for key in document["packages"] if key)
            document["packages"][dependency]["version"] = "99.0.0"
        text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    else:
        owned = set(prepare_release._release_version_spans(relative, text))
        matches = [
            match
            for match in re.finditer(r'version = "([^"\n]+)"', text)
            if match.span(1) not in owned
        ]
        assert matches, "the ordinary public fixture must contain an actual dependency version"
        match = matches[0]
        text = text[: match.start(1)] + "99.0.0" + text[match.end(1) :]
    source.write_text(text)
    _git(repo, "add", "--", relative)
    tree = _git(repo, "write-tree")
    head = _git(
        repo, "commit-tree", tree, "-p", main, "-p", feature, "-m", "merge: dependency tampering"
    )
    with pytest.raises(
        ValueError, match="unexplained content|no clean combination|no commit intent"
    ):
        prepare_release.commits_for_net_diff(repo, main, head)


@pytest.mark.parametrize("relative", ["openadapt_desktop/__init__.py", "src/feature.txt"])
def test_generated_version_resolution_never_absorbs_a_file_mode_change(
    repo: Path, relative: str
) -> None:
    _start, feature, main = _version_merge_branches(repo)
    _resolve_version_merge(repo, feature, main, first="feature")
    _git(repo, "update-index", "--chmod=+x", "--", relative)
    tree = _git(repo, "write-tree")
    head = _git(
        repo,
        "commit-tree",
        tree,
        "-p",
        feature,
        "-p",
        main,
        "-m",
        "merge: unexplained executable mode",
    )
    with pytest.raises(ValueError, match="file modes|unexplained content|no clean combination"):
        prepare_release.commits_for_net_diff(repo, main, head)


# Release-history selection oracle: equal generated versions are not evidence
# that either merge side was published. The explicit base is only a comparison.
def _boundary_outputs(repo: Path) -> dict[str, bytes]:
    return {name: (repo / name).read_bytes() for name in VERSION_FILES}


def _boundary_patch(version: str) -> str:
    major, minor, patch = map(int, version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def _boundary_merge(repo: Path, other: str, published: str, name: str) -> str:
    result = subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", other], cwd=repo, capture_output=True
    )
    assert result.returncode in {0, 1}, result.stderr
    assert set(_git(repo, "diff", "--name-only", "--diff-filter=U").splitlines()) <= {
        "CHANGELOG.md"
    }
    # Both sides prepared the same version. Keep the declared comparison
    # side's existing notes; the next normal preparation creates new notes.
    notes = subprocess.check_output(["git", "show", f"{published}:./CHANGELOG.md"], cwd=repo)
    (repo / "CHANGELOG.md").write_bytes(notes)
    return _commit(repo, name)


def _equal_version_history(
    repo: Path, monkeypatch, published_side: str, first: str, clock: str, merges: int
) -> tuple[str, str, str]:
    start = _git(repo, "rev-parse", "HEAD")
    dates = {
        "left-newer": {"left": "2040-01-01T00:00:00Z", "right": "2001-01-01T00:00:00Z"},
        "right-newer": {"left": "2001-01-01T00:00:00Z", "right": "2040-01-01T00:00:00Z"},
        "equal": {"left": "2026-01-01T00:00:00Z", "right": "2026-01-01T00:00:00Z"},
    }
    tips = {}
    for side in ("left", "right"):
        _git(repo, "checkout", "-q", "-b", "boundary-" + side, start)
        monkeypatch.setenv("GIT_AUTHOR_DATE", dates[clock][side])
        monkeypatch.setenv("GIT_COMMITTER_DATE", dates[clock][side])
        message = "already shipped rollback" if side == published_side else "retain unshipped fix"
        _commit(repo, f"fix({side}): {message}", touch=f"src/{side}.txt")
        prepare_release.prepare(repo, today=datetime.date(2026, 11, 1))
        tips[side] = _commit(repo, "chore: prepare equal engine version")
    other = "right" if first == "left" else "left"
    _git(repo, "checkout", "-q", "-b", "boundary-integration", tips[first])
    published = tips[published_side]
    _boundary_merge(repo, tips[other], published, "merge: combine equal versions")
    if merges == 2:
        _git(repo, "checkout", "-q", "boundary-" + published_side)
        published = _commit(repo, "docs: retain published release metadata", touch="docs/release.txt")
        _git(repo, "checkout", "-q", "boundary-integration")
        _commit(repo, "fix: retain an additional unshipped correction", touch="src/additional.txt")
        _boundary_merge(repo, published, published, "merge: combine equal versions again")
    return published, "right" if published_side == "left" else "left", _version(repo)


@pytest.mark.parametrize("published_side", ["left", "right"])
@pytest.mark.parametrize("first", ["left", "right"])
@pytest.mark.parametrize("clock", ["left-newer", "right-newer", "equal"])
@pytest.mark.parametrize("merges", [1, 2])
def test_equal_version_history_requires_explicit_comparison_base(
    repo: Path, monkeypatch, published_side: str, first: str, clock: str, merges: int
) -> None:
    base, unshipped_side, current = _equal_version_history(
        repo, monkeypatch, published_side, first, clock, merges
    )
    before = _boundary_outputs(repo)
    for options in ({}, {"level": "patch"}, {"version": _boundary_patch(current)}):
        with pytest.raises(ValueError, match="ambiguous.*--base"):
            prepare_release.prepare(repo, **options)
        assert _boundary_outputs(repo) == before
    selected = prepare_release._release_commits(repo, base, current)
    assert f"fix({unshipped_side}): retain unshipped fix" in {c.subject for c in selected}
    assert not any("already shipped rollback" in c.subject for c in selected)
    problems = check_release_intent.check(repo, base, _git(repo, "rev-parse", "HEAD"))
    assert len(problems) == 1 and f"--base {base}" in problems[0]
    assert "--bump patch" in problems[0]
    if (published_side, first, clock, merges) == ("left", "left", "left-newer", 1):
        monkeypatch.setattr(prepare_release, "ROOT", repo)
        assert prepare_release.main(["--base", base]) == 0
        actual = _version(repo)
    else:
        actual = prepare_release.prepare(repo, base=base, today=datetime.date(2026, 11, 8))
    assert actual == _boundary_patch(current)
    notes = (repo / "CHANGELOG.md").read_text()
    new_section = notes.split("## v", 2)[1]
    assert f"**{unshipped_side}**: retain unshipped fix" in new_section
    assert "already shipped rollback" not in new_section
    if merges == 2:
        assert "retain an additional unshipped correction" in new_section
    head = _commit(repo, "chore: prepare the next engine patch")
    assert check_release_intent.check(repo, base, head) == []


@pytest.mark.parametrize(
    "shape",
    [
        "missing-full-oid", "abbreviated-oid", "symbolic-ref", "nonancestor-commit",
        "unrelated-root", "pyproject-absent", "pyproject-nonregular",
        "wrong-project-identity", "duplicate-or-malformed-version", "base-current-version-mismatch",
    ],
)
def test_explicit_release_base_refuses_invalid_history_before_writing(repo: Path, shape: str) -> None:
    start = _git(repo, "rev-parse", "HEAD")
    original = (repo / "pyproject.toml").read_bytes()
    _git(repo, "checkout", "-q", "-b", "base-candidate")
    base = start
    if shape == "nonancestor-commit":
        _git(repo, "checkout", "-q", "-b", "unmerged-base", start)
        base = _commit(repo, "fix: unmerged side", touch="src/side.txt")
        _git(repo, "checkout", "-q", "base-candidate")
    elif shape == "unrelated-root":
        _git(repo, "checkout", "-q", "--orphan", "unrelated-base")
        base = _commit(repo, "chore: unrelated root")
        _git(repo, "checkout", "-q", "base-candidate")
    elif shape in {"pyproject-absent", "pyproject-nonregular"}:
        _git(repo, "rm", "-q", "pyproject.toml")
        if shape == "pyproject-nonregular":
            (repo / "project-target.toml").write_bytes(original)
            (repo / "pyproject.toml").symlink_to("project-target.toml")
        base = _commit(repo, "chore: invalid historical project path")
        if shape == "pyproject-nonregular":
            _git(repo, "rm", "-q", "pyproject.toml")
        (repo / "pyproject.toml").write_bytes(original)
    elif shape in {"wrong-project-identity", "duplicate-or-malformed-version"}:
        text = original.decode()
        if shape == "wrong-project-identity":
            text = text.replace('name = "openadapt-desktop"', 'name = "different-project"', 1)
        else:
            text = text.replace(f'version = "{_version(repo)}"', 'version = "01.2.3"', 1)
        (repo / "pyproject.toml").write_text(text)
        base = _commit(repo, "chore: invalid historical project metadata")
        (repo / "pyproject.toml").write_bytes(original)
    elif shape == "base-current-version-mismatch":
        prepare_release.set_engine_version(repo, _boundary_patch(_version(repo)))
    _commit(repo, "fix: a genuine pending correction", touch="src/pending.txt")
    if shape == "missing-full-oid":
        base = "f" * 40
    elif shape == "abbreviated-oid":
        base = start[:12]
    elif shape == "symbolic-ref":
        base = "HEAD"
    before = _boundary_outputs(repo)
    for options in ({}, {"level": "patch"}, {"version": _boundary_patch(_version(repo))}):
        with pytest.raises(ValueError):
            prepare_release.prepare(repo, base=base, **options)
        assert _boundary_outputs(repo) == before


@pytest.mark.parametrize(
    "shape",
    [
        "ordinary-linear-feature", "ordinary-linear-fix", "ordinary-docs-only-refusal",
        "ordinary-explicit-level-existing-semantics", "ordinary-explicit-version-existing-semantics",
        "linear-clock-skew-ancestry-maximal-origin", "explicit-base-cancelled-fix-no-surviving-bump",
        "explicit-base-unexplained-merge-refusal", "dependency-version-text-not-engine-origin",
    ],
)
def test_release_boundary_preserves_ordinary_calls_and_lineage_guards(
    repo: Path, monkeypatch, shape: str
) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    current = _version(repo)
    options = {}
    if shape == "ordinary-linear-feature":
        _commit(repo, "feat: retain a normal feature", touch="src/feature.txt")
    elif shape == "ordinary-docs-only-refusal":
        _commit(repo, "docs: only explain", touch="docs/words.md")
    elif shape.startswith("ordinary-explicit-"):
        _commit(repo, "docs: only explain", touch="docs/words.md")
        options = {"level": "patch"} if "-level-" in shape else {"version": _boundary_patch(current)}
    elif shape == "explicit-base-cancelled-fix-no-surviving-bump":
        _commit(repo, "fix: a cancelled feature", touch="src/cancelled.txt")
        _git(repo, "rm", "-q", "src/cancelled.txt")
        _commit(repo, "fix: remove the cancelled feature")
        options = {"base": base}
    elif shape == "explicit-base-unexplained-merge-refusal":
        (repo / "src").mkdir(exist_ok=True)
        (repo / "src/shared.txt").write_text("baseline\n")
        start = _commit(repo, "chore: establish a merge source")
        _git(repo, "checkout", "-q", "-b", "unexplained-left")
        _commit(repo, "fix: left change", touch="src/shared.txt")
        left = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-q", "-b", "unexplained-right", start)
        base = _commit(repo, "fix: right change", touch="src/shared.txt")
        result = subprocess.run(["git", "merge", "--no-ff", "--no-commit", left], cwd=repo, capture_output=True)
        assert result.returncode == 1
        (repo / "src/shared.txt").write_text("unexplained manual bytes\n")
        _commit(repo, "merge: invent an unexplained resolution")
        options = {"base": base}
    elif shape == "dependency-version-text-not-engine-origin":
        path = repo / "pyproject.toml"
        path.write_text(path.read_text() + '\n[tool.synthetic]\nversion = "8.9.0"\n')
        _commit(repo, "fix: update dependency metadata")
        assert prepare_release.last_version_change(repo) == base
    else:
        if shape == "linear-clock-skew-ancestry-maximal-origin":
            monkeypatch.setenv("GIT_AUTHOR_DATE", "2001-01-01T00:00:00Z")
            monkeypatch.setenv("GIT_COMMITTER_DATE", "2000-01-01T00:00:00Z")
        _commit(repo, "fix: retain a normal correction", touch="src/correction.txt")
    before = _boundary_outputs(repo)
    if shape in {
        "ordinary-docs-only-refusal", "explicit-base-cancelled-fix-no-surviving-bump",
        "explicit-base-unexplained-merge-refusal",
    }:
        with pytest.raises(ValueError):
            prepare_release.prepare(repo, **options)
        assert _boundary_outputs(repo) == before
        return
    actual = prepare_release.prepare(repo, today=datetime.date(2026, 11, 8), **options)
    major, minor, _patch = map(int, current.split("."))
    expected = f"{major}.{minor + 1}.0" if shape == "ordinary-linear-feature" else _boundary_patch(current)
    assert actual == expected
    version_commit = _commit(repo, "chore: prepare a normal engine version")
    assert prepare_release.last_version_change(repo) == version_commit
    if shape == "dependency-version-text-not-engine-origin":
        assert '[tool.synthetic]\nversion = "8.9.0"' in (repo / "pyproject.toml").read_text()
    if not shape.startswith("ordinary-explicit-"):
        assert check_release_intent.check(repo, base, version_commit) == []
