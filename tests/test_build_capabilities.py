"""CAPABILITIES.md is assembled from capabilities/, one file per table row.

A pull request adds or edits its own row file and leaves the generated pages
(CAPABILITIES.md, llms-full.txt) alone; the refresh workflow regenerates them
after the change lands. These tests keep the sources well formed and the
pull-request check honest: it passes a stale page, and fails a hand edit that
the next refresh would drop.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_capabilities.py"


@pytest.fixture()
def mod():
    spec = importlib.util.spec_from_file_location("build_capabilities_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_sources_assemble_and_split_back_to_the_same_files(mod, tmp_path):
    page = mod.assemble()
    mod.write_split(page, tmp_path / "capabilities")
    assert mod.assemble(tmp_path / "capabilities") == page
    assert page.startswith("# What OpenAdapt can do\n")


def test_every_table_row_is_its_own_file(mod):
    page = mod.assemble()
    files = sorted(mod.SOURCES.glob("*/[0-9]*.md"))
    assert files, "no row files under capabilities/"
    for path in files:
        line = path.read_text("utf-8").rstrip("\n")
        assert page.count(line + "\n") >= 1, path
    # No table row lives in a section's prose.
    for section in mod.SOURCES.glob(f"*/{mod.SECTION}"):
        text = section.read_text("utf-8").splitlines()
        rows = [line for line in text if line.startswith("|")]
        assert len(rows) in (0, 2), f"{section}: keep only the table header here"


def _section(tmp_path: Path, rows: dict[str, str]) -> Path:
    root = tmp_path / "capabilities"
    section = root / "01-a"
    section.mkdir(parents=True)
    (section / "section.md").write_text(
        "## A\n\n| X | Y |\n| --- | --- |\n<!-- capability rows -->\n\nAfter.\n",
        "utf-8",
    )
    for name, text in rows.items():
        (section / name).write_text(text, "utf-8")
    return root


def test_rows_print_by_number_then_name(mod, tmp_path):
    root = _section(
        tmp_path,
        {
            "0020-b.md": "| b | 2 |\n",
            "0100-z.md": "| z | 10 |\n",
            "0020-a.md": "| a | 2 |\n",
            "0005-first.md": "| first | 0 |",
        },
    )
    assert mod.assemble(root) == (
        "## A\n\n| X | Y |\n| --- | --- |\n"
        "| first | 0 |\n| a | 2 |\n| b | 2 |\n| z | 10 |\n\nAfter.\n"
    )


@pytest.mark.parametrize(
    "name,text",
    [
        ("0010-two-rows.md", "| a | 1 |\n| b | 2 |\n"),
        ("0010-prose.md", "not a row\n"),
        ("Row.md", "| a | 1 |\n"),
    ],
)
def test_a_malformed_row_file_fails_the_build(mod, tmp_path, name, text):
    with pytest.raises(SystemExit):
        mod.assemble(_section(tmp_path, {name: text}))


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def test_the_pr_check_passes_stale_pages_and_fails_hand_edits(
    mod, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    page, brief = repo / "CAPABILITIES.md", repo / "llms-full.txt"
    page.write_text("old page\n", "utf-8")
    brief.write_text("old brief\n", "utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    monkeypatch.setattr(mod, "ROOT", repo)
    monkeypatch.setattr(
        mod, "generated", lambda: {page: "new page\n", brief: "new brief\n"}
    )
    main = mod.main

    # The sources changed and the pages trail them: a normal pull request.
    assert main(["--check-pr", base]) == 0
    # Regenerated in the pull request: allowed (it may conflict, though).
    page.write_text("new page\n", "utf-8")
    assert main(["--check-pr", base]) == 0
    # A hand edit, which the next refresh would silently drop.
    page.write_text("old page\n| a hand-added row |\n", "utf-8")
    assert main(["--check-pr", base]) == 1
    page.write_text("old page\n", "utf-8")
    brief.write_text("old brief, edited\n", "utf-8")
    assert main(["--check-pr", base]) == 1


def test_the_committed_stamp_matches_the_page_the_sources_build(mod):
    # Seeded with the split; every regeneration rewrites it with the page.
    assert mod.guard() == 0


def test_the_guard_stops_a_refresh_that_would_drop_a_hand_edit(
    mod, tmp_path, monkeypatch, capsys
):
    page, stamp = tmp_path / "CAPABILITIES.md", tmp_path / "stamp"
    monkeypatch.setattr(mod, "OUT", page)
    monkeypatch.setattr(mod, "STAMP", stamp)
    monkeypatch.setattr(mod, "assemble", lambda: "| a |\n| b |\n")
    page.write_text("| a |\n", "utf-8")
    stamp.write_text(mod._stamp("| a |\n"), "utf-8")
    # The page trails its sources (a new row file): the refresh may go on.
    assert mod.guard() == 0
    # A pull request whose CI predates --check-pr added a row to the page.
    page.write_text("| a |\n| hand-added |\n", "utf-8")
    assert mod.guard() == 1
    assert "| hand-added |" in capsys.readouterr().err
