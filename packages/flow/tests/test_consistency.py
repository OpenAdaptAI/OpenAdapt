"""Drive the claims-consistency gate (`scripts/check_consistency.py`).

Two kinds of assertion:

* the REAL tree is consistent right now (`run_all_checks()` is empty), which is
  what the CI step enforces on every PR; and
* each individual check actually FAILS on injected drift, so the gate cannot
  rot into a no-op.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_consistency.py"
_spec = importlib.util.spec_from_file_location("check_consistency", _SCRIPT)
assert _spec and _spec.loader
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


# --------------------------------------------------------------------------- #
# the real tree is consistent
# --------------------------------------------------------------------------- #
def test_real_tree_is_consistent():
    errors = cc.run_all_checks()
    assert errors == [], "consistency gate found drift:\n" + "\n".join(errors)


def test_main_exits_zero():
    assert cc.main() == 0


def test_readme_and_pyproject_versions_agree():
    assert cc.package_version() == cc.pyproject_version()


def test_lock_and_pyproject_versions_agree():
    assert cc.lock_version() == cc.pyproject_version()


# --------------------------------------------------------------------------- #
# version drift is caught
# --------------------------------------------------------------------------- #
def test_version_mismatch_detected():
    assert cc.check_version(pkg="9.9.9", toml="0.0.1")


def test_version_match_ok():
    assert cc.check_version(pkg="1.2.3", toml="1.2.3") == []


def test_lock_version_mismatch_detected():
    assert cc.check_lock_version(lock="1.2.2", toml="1.2.3")


def test_lock_version_match_ok():
    assert cc.check_lock_version(lock="1.2.3", toml="1.2.3") == []


# --------------------------------------------------------------------------- #
# banned phrases are caught
# --------------------------------------------------------------------------- #
def test_banned_phrase_detected():
    for phrase in (
        "vision-only",
        "adapters to come",
        "864 tests",
        "any repeated GUI task",
        "demonstrated once",
        "replays exactly",
    ):
        errors = cc.check_banned_phrases(f"prose {phrase} more prose")
        assert errors, f"{phrase!r} not flagged"


def test_clean_readme_has_no_banned_phrase():
    assert cc.check_banned_phrases("vision-first, self-healing replay") == []


def test_real_readme_is_clean_of_banned_phrases():
    assert cc.check_banned_phrases() == []


# --------------------------------------------------------------------------- #
# test-count check: absent number passes without collecting; wrong number fails
# --------------------------------------------------------------------------- #
def test_no_hardcoded_count_skips_collection():
    called = []

    def boom() -> int:
        called.append(True)
        return 0

    assert cc.check_test_count("no number here", count_fn=boom) == []
    assert not called, "collection ran despite no README count"


def test_wrong_hardcoded_count_detected():
    errors = cc.check_test_count("this repo has 42 tests today", count_fn=lambda: 1303)
    assert errors


def test_close_count_within_tolerance_ok():
    errors = cc.check_test_count(
        "about 1300 tests", count_fn=lambda: 1303, tolerance=25
    )
    assert errors == []


def test_real_readme_omits_hardcoded_count():
    # The chosen drift-proof state: the README carries no `<n> tests` claim.
    import re

    assert not re.search(r"\d[\d,]*\s+tests\b", cc.read(cc.README))


# --------------------------------------------------------------------------- #
# path check: real refs resolve; prose slashes are ignored; bad refs fail
# --------------------------------------------------------------------------- #
def test_real_docs_reference_only_existing_paths():
    assert cc.check_paths() == []


def test_prose_slashes_are_not_treated_as_paths():
    # ext-less prose, verb lists, and glyph pairs must not be flagged.
    for junk in ("O/0", "20/20", "save/submit/create/delete/", "tool/MCP"):
        assert cc._is_path_like(junk, from_link=False) is None


def test_bare_filename_without_slash_ignored():
    # format sketches like `workflow.json` (no directory) are not repo files.
    assert cc._is_path_like("workflow.json", from_link=False) is None


def test_missing_backtick_path_detected():
    assert cc._is_path_like("openadapt_flow/does_not_exist.py", from_link=False)
    assert not cc._exists("openadapt_flow/does_not_exist.py", cc.REPO_ROOT)


def test_existing_path_and_glob_resolve():
    assert cc._exists("openadapt_flow/ir.py", cc.REPO_ROOT)
    # a `dir/**` glob resolves to its directory
    norm = cc._is_path_like("openadapt_flow/emit/**", from_link=False)
    assert norm == "openadapt_flow/emit"
    assert cc._exists(norm, cc.REPO_ROOT)


def test_leading_dot_path_preserved():
    norm = cc._is_path_like(".github/dependabot.yml", from_link=False)
    assert norm == ".github/dependabot.yml"
    assert cc._exists(norm, cc.REPO_ROOT)


def test_urls_are_not_paths():
    assert (
        cc._is_path_like("https://img.shields.io/pypi/v/openadapt-flow", True) is None
    )
