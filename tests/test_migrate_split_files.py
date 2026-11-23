"""scripts/migrate_branch_to_split_files.py moves a branch onto the split files."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts/migrate_branch_to_split_files.py"
)
_spec = importlib.util.spec_from_file_location("migrate_split_files", SCRIPT)
assert _spec and _spec.loader
migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate)

CLAIMS = """\
# header comment
version: 1

claims:
  # ---------------------------------------------- web
  - id: web-supported
    claim: >-
      Records and replays.
    tier: supported
    evidence:
      # why this test
      - path: tests/test_web.py
        proves: it works

  # ---------------------------------------------- new
  - id: new-claim
    claim: A new one.
    tier: roadmap
"""

OLD_LIST = """\
## Every command, by capability

Intro.

- **Record and build a program:** `record`, `compile`.
- **Judge any executor's write:** `verify-run`, `serve-reward` (scores agent episodes).

## Measured results
"""


def test_split_claims_keeps_each_claims_wording() -> None:
    pytest.importorskip("yaml")
    split = migrate.split_claims(CLAIMS)
    assert list(split) == ["web-supported", "new-claim"]
    assert split["web-supported"].startswith("id: web-supported\nclaim: >-\n")
    assert "# why this test" in split["web-supported"]
    assert "# ----" not in split["web-supported"]
    assert split["new-claim"] == "id: new-claim\nclaim: A new one.\ntier: roadmap\n"


def test_parse_command_list_reads_groups_and_notes() -> None:
    commands = migrate.parse_command_list(OLD_LIST)
    assert commands == {
        "record": ("Record and build a program", ""),
        "compile": ("Record and build a program", ""),
        "verify-run": ("Judge any executor's write", ""),
        "serve-reward": ("Judge any executor's write", "scores agent episodes"),
    }
    assert migrate._note_text("scores agent episodes") == "Scores agent episodes.\n"
    assert migrate.parse_command_list("# no list here\n") == {}


def test_resolve_takes_main_only_inside_the_command_list() -> None:
    text = (
        "## Map\n"
        "<<<<<<< HEAD\n| branch row |\n=======\n| main row |\n>>>>>>> origin/main\n"
        "## Every command, by capability\n"
        "<<<<<<< HEAD\n"
        "- **Run:** `run`, `new-verb`.\n"
        "||||||| base\n"
        "- **Run:** `run`.\n"
        "=======\n"
        f"{migrate.GENERATED_BEGIN}\n- `run`\n{migrate.GENERATED_END}\n"
        ">>>>>>> origin/main\n"
        "## Measured results\n"
    )
    resolved = migrate.resolve_command_hunks(text)
    assert "new-verb" not in resolved
    assert migrate.GENERATED_BEGIN in resolved
    # The map-row conflict is not ours to decide.
    assert "<<<<<<< HEAD\n| branch row |" in resolved
    assert resolved.count("<<<<<<<") == 1


PAGE = """\
# Title

Intro.

## Core

| Capability | Status |
| --- | --- |
| One | Released |
| Two | Released |
| Three | Released |

## Other

Prose.
"""


def _git(cwd: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")


def test_capability_rows_move_to_row_files(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, {"CAPABILITIES.md": PAGE, "llms-full.txt": "brief\n"})
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    # The branch, from before the split: adds a row after Two, edits Three.
    _git(repo, "checkout", "-qb", "feature")
    branch_page = PAGE.replace(
        "| Two | Released |\n", "| Two | Released |\n| Mine | On main |\n"
    ).replace("| Three | Released |", "| Three | Released, faster |")
    _write(repo, {"CAPABILITIES.md": branch_page, "llms-full.txt": "brief, mine\n"})
    _git(repo, "commit", "-qam", "feature")
    # main: the split, and another pull request's new row (page not refreshed).
    _git(repo, "checkout", "-q", "main")
    _write(
        repo,
        {
            "capabilities/00-title/section.md": "# Title\n\nIntro.\n\n",
            "capabilities/01-core/section.md": (
                "## Core\n\n| Capability | Status |\n| --- | --- |\n"
                "<!-- capability rows -->\n\n"
            ),
            "capabilities/01-core/0010-one.md": "| One | Released |\n",
            "capabilities/01-core/0020-two.md": "| Two | Released |\n",
            "capabilities/01-core/0030-three.md": "| Three | Released |\n",
            "capabilities/01-core/0040-four.md": "| Four | Released |\n",
            "capabilities/02-other/section.md": "## Other\n\nProse.\n",
        },
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "split")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "--no-commit", "--no-ff", "main")

    monkeypatch.chdir(repo)
    assert migrate.main([]) == 0
    core = repo / "capabilities/01-core"
    rows = sorted(p.name for p in core.glob("0*.md"))
    assert rows == [
        "0010-one.md",
        "0020-two.md",
        "0025-mine.md",
        "0030-three.md",
        "0040-four.md",
    ]
    assert (core / "0025-mine.md").read_text() == "| Mine | On main |\n"
    assert (core / "0030-three.md").read_text() == "| Three | Released, faster |\n"
    # The generated pages are main's; the refresh workflow rebuilds them.
    assert (repo / "CAPABILITIES.md").read_text() == PAGE
    assert (repo / "llms-full.txt").read_text() == "brief\n"
