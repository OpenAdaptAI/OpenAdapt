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
