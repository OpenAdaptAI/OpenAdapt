"""Git/CI scanner: OHIP, chart, UNC, RDP, path bans, fail-closed self-test."""

from __future__ import annotations

import ast
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


def test_scan_source_has_no_personal_name() -> None:
    assert "personal_name_" not in SCAN_PY.read_text(encoding="utf-8")


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
