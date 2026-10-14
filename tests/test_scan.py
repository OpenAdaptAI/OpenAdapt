"""Git/CI scanner: OHIP, chart, UNC, RDP, path bans, fail-closed self-test."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from openadapt_privacy.scan import (
    FORBIDDEN_DIR_PARTS,
    FORBIDDEN_SUFFIXES,
    RULES,
    main,
    plant_fixtures,
    scan_tree,
    self_test,
)

ROOT = Path(__file__).resolve().parents[1]
SCAN_PY = ROOT / "openadapt_privacy" / "scan.py"


def _git_init(path: Path) -> None:
    subprocess.run(
        ["git", "init"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def _stage(path: Path) -> None:
    subprocess.run(
        ["git", "add", "-A", "-f"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def _hit_ids(hits: list[str]) -> set[str]:
    return {h.split("\t", 1)[0] for h in hits}


def test_scan_module_is_stdlib_only() -> None:
    source = SCAN_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                pytest.fail("scan.py must not use relative imports")
            if node.module:
                imported.add(node.module.split(".", 1)[0])
    imported.discard("__future__")
    assert imported <= set(sys.stdlib_module_names)
    for banned in ("PIL", "pillow", "presidio", "spacy", "openadapt_privacy"):
        assert banned not in imported


def test_scan_source_has_no_kirill() -> None:
    assert "kirill_" not in SCAN_PY.read_text(encoding="utf-8")


def test_scan_source_has_no_matching_ohip_example() -> None:
    source = SCAN_PY.read_text(encoding="utf-8")
    assert re.search(r"\b\d{4}-\d{3}-\d{3}\b", source) is None
    assert re.search(r"\b\d{4} \d{3} \d{3}\b", source) is None
    assert re.search(r"\b\d{4}\.\d{3}\.\d{3}\b", source) is None


def test_import_scan_does_not_load_pillow_presidio_or_spacy() -> None:
    code = r"""
import builtins
import sys

real_import = builtins.__import__
blocked = (
    "PIL",
    "presidio_analyzer",
    "presidio_anonymizer",
    "presidio_image_redactor",
    "spacy",
)

def guarded(name, *args, **kwargs):
    root = name.split(".", 1)[0]
    if name in blocked or root in blocked:
        raise ImportError(f"blocked {name}")
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded
import openadapt_privacy.scan as scan
assert callable(scan.scan_tree)
assert callable(scan.self_test)
for name in blocked:
    assert name not in sys.modules, name
print("ok")
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


def test_self_test_passes() -> None:
    assert self_test() == 0


def test_self_test_fail_closed_when_ohip_rule_is_blind(monkeypatch: pytest.MonkeyPatch) -> None:
    blinded = [
        (rule_id, re.compile(r"(?!x)x") if rule_id == "ohip-dashed" else pattern)
        for rule_id, pattern in RULES
    ]
    monkeypatch.setattr("openadapt_privacy.scan.RULES", blinded)
    assert self_test() == 1


def test_self_test_fail_closed_when_rdp_suffix_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openadapt_privacy.scan.FORBIDDEN_SUFFIXES",
        FORBIDDEN_SUFFIXES - {".rdp"},
    )
    assert self_test() == 1


def test_scan_tree_fires_each_planted_rule(tmp_path: Path) -> None:
    _git_init(tmp_path)
    planted = plant_fixtures(tmp_path)
    _stage(tmp_path)
    hits = scan_tree(tmp_path)
    fired = _hit_ids(hits)
    for rule_id, path in planted.items():
        if rule_id == "rdp-file":
            assert any(
                h.startswith("forbidden-suffix\t") and h.endswith(".rdp") for h in hits
            ), hits
            continue
        rel = path.name
        assert any(h.startswith(f"{rule_id}\t{rel}:") for h in hits), (rule_id, hits)
    assert "ohip-dashed" in fired
    assert "chart-id-assigned" in fired
    assert "mrn-assigned" in fired
    assert "unc-path" in fired
    assert "rdp-full-address" in fired
    assert "rdp-env-host" in fired
    assert "mstsc-host" in fired


@pytest.mark.parametrize(
    "dirname",
    sorted(FORBIDDEN_DIR_PARTS),
)
def test_forbidden_dir_parts(tmp_path: Path, dirname: str) -> None:
    _git_init(tmp_path)
    nested = tmp_path / dirname
    nested.mkdir()
    (nested / "note.txt").write_text("ok\n", encoding="utf-8")
    _stage(tmp_path)
    hits = scan_tree(tmp_path)
    assert any(h.startswith("forbidden-dir\t") and dirname in h for h in hits), hits


@pytest.mark.parametrize("suffix", [".rdp", ".db", ".png", ".mp4"])
def test_forbidden_suffixes(tmp_path: Path, suffix: str) -> None:
    _git_init(tmp_path)
    (tmp_path / f"artifact{suffix}").write_text("x\n", encoding="utf-8")
    _stage(tmp_path)
    hits = scan_tree(tmp_path)
    assert any(h.startswith("forbidden-suffix\t") and h.endswith(suffix) for h in hits), hits


def test_clean_tree_has_no_hits(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "readme.txt").write_text("no identifiers here\n", encoding="utf-8")
    _stage(tmp_path)
    assert scan_tree(tmp_path) == []


def test_cli_self_test() -> None:
    assert main(["--self-test"]) == 0


def test_cli_root_exits_one_on_hits(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plant_fixtures(tmp_path)
    _stage(tmp_path)
    assert main(["--root", str(tmp_path)]) == 1


def test_cli_root_exits_zero_when_clean(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "ok.txt").write_text("hello\n", encoding="utf-8")
    _stage(tmp_path)
    assert main(["--root", str(tmp_path)]) == 0


def test_cli_missing_root_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--root", str(tmp_path / "nope")]) == 2
    captured = capsys.readouterr()
    assert "does not exist" in captured.err
    assert "clean" not in captured.out + captured.err


def test_cli_file_root_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "file.txt"
    target.write_text("hello\n", encoding="utf-8")
    assert main(["--root", str(target)]) == 2
    captured = capsys.readouterr()
    assert "not a directory" in captured.err
    assert "clean" not in captured.out + captured.err


@pytest.mark.parametrize("use_git", [False, True], ids=["plain-dir", "git-repo"])
def test_cli_empty_root_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], use_git: bool
) -> None:
    if use_git:
        _git_init(tmp_path)
    assert main(["--root", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert "no files" in captured.err
    assert "clean" not in captured.out + captured.err


def test_cli_empty_root_passes_with_allow_empty(tmp_path: Path) -> None:
    assert main(["--root", str(tmp_path), "--allow-empty"]) == 0


def test_scan_tree_missing_root_raises(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        scan_tree(tmp_path / "nope")


def _skip_if_root_user() -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can read files whatever their permissions")


@pytest.mark.parametrize("use_git", [False, True], ids=["plain-dir", "git-repo"])
def test_cli_unreadable_subdirectory_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], use_git: bool
) -> None:
    _skip_if_root_user()
    if use_git:
        _git_init(tmp_path)
    (tmp_path / "ok.txt").write_text("hello\n", encoding="utf-8")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "note.txt").write_text("hello\n", encoding="utf-8")
    locked.chmod(0)
    try:
        code = main(["--root", str(tmp_path)])
    finally:
        locked.chmod(0o700)
    assert code == 2
    captured = capsys.readouterr()
    assert "could not list files" in captured.err
    assert "clean" not in captured.out + captured.err


@pytest.mark.parametrize("use_git", [False, True], ids=["plain-dir", "git-repo"])
def test_cli_unsearchable_subdirectory_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], use_git: bool
) -> None:
    # Read but no execute permission: the names list, but the files can't be
    # opened. Python 3.14's Path.is_file() returns False here instead of raising.
    _skip_if_root_user()
    if use_git:
        _git_init(tmp_path)
    (tmp_path / "ok.txt").write_text("hello\n", encoding="utf-8")
    half = tmp_path / "half"
    half.mkdir()
    (half / "note.txt").write_text("hello\n", encoding="utf-8")
    half.chmod(0o444)
    try:
        code = main(["--root", str(tmp_path)])
    finally:
        half.chmod(0o700)
    assert code == 2
    captured = capsys.readouterr()
    assert "could not list files" in captured.err
    assert "clean" not in captured.out + captured.err


def test_cli_root_under_unsearchable_parent_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _skip_if_root_user()
    locked = tmp_path / "locked"
    root = locked / "sub"
    root.mkdir(parents=True)
    (root / "ok.txt").write_text("hello\n", encoding="utf-8")
    locked.chmod(0)
    try:
        code = main(["--root", str(root)])
    finally:
        locked.chmod(0o700)
    assert code == 2
    captured = capsys.readouterr()
    assert "can't be accessed" in captured.err
    assert "clean" not in captured.out + captured.err


def test_cli_unreadable_global_git_ignore_still_scans(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # git warns "unable to access ...: Permission denied" for an unreadable
    # global ignore file, but it still lists every file. That's not a failed scan.
    _skip_if_root_user()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "ok.txt").write_text("hello\n", encoding="utf-8")
    home = tmp_path / "home"
    ignore = home / ".config" / "git" / "ignore"
    ignore.parent.mkdir(parents=True)
    ignore.write_text("*.txt\n", encoding="utf-8")
    ignore.chmod(0)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    try:
        code = main(["--root", str(repo)])
    finally:
        ignore.chmod(0o600)
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "PHI scan clean (1 files)" in captured.out


def test_cli_unreadable_file_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _skip_if_root_user()
    (tmp_path / "ok.txt").write_text("hello\n", encoding="utf-8")
    locked = tmp_path / "locked.txt"
    locked.write_text("hello\n", encoding="utf-8")
    locked.chmod(0)
    try:
        code = main(["--root", str(tmp_path)])
    finally:
        locked.chmod(0o600)
    assert code == 1
    captured = capsys.readouterr()
    assert "unreadable\tlocked.txt" in captured.err


def test_module_cli_self_test() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "openadapt_privacy.scan", "--self-test"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "self-test ok:" in completed.stdout


def test_scan_extra_is_empty() -> None:
    source = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"(?m)^scan\s*=\s*\[(.*?)\]\s*$", source)
    assert match is not None, source
    body = re.sub(r"#.*", "", match.group(1))
    assert re.search(r"[A-Za-z0-9]", body) is None


def test_console_script_is_declared() -> None:
    source = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'openadapt-privacy-scan = "openadapt_privacy.scan:main"' in source


def test_scan_is_not_reexported_from_package_init() -> None:
    import openadapt_privacy

    assert "scan" not in openadapt_privacy.__all__
    assert "scan_tree" not in openadapt_privacy.__all__
    assert "scan" not in openadapt_privacy._LAZY_EXPORTS
    assert "scan_tree" not in openadapt_privacy._LAZY_EXPORTS


def test_readme_keeps_presidio_caveat_and_has_scan_section() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Git / CI scan (`[scan]`)" in readme
    assert "synthetic, not clinical" in readme
    assert "does not claim clinical validation" in readme
    assert "--allow-empty" in readme
