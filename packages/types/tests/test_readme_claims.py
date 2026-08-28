"""The README states facts about the package. Keep them true.

A count written by hand goes stale the moment a schema is added or removed,
and a reader has no way to tell. These tests read the claim out of the README
and check it against the shipped tree.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
SCHEMA_DIR = ROOT / "openadapt_types" / "schemas"

# Indexed by value, so WORDS[17] == "seventeen". Extend it when the directory
# grows past the end rather than dropping the count from the README.
WORDS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
    "twenty-one", "twenty-two", "twenty-three", "twenty-four", "twenty-five",
    "twenty-six", "twenty-seven", "twenty-eight", "twenty-nine", "thirty",
)

# Tied to the sentence itself, so an unrelated "N files" elsewhere in the
# README cannot satisfy this test by accident.
SCHEMA_COUNT_CLAIM = re.compile(
    r"([A-Za-z][A-Za-z-]*) files, including\s+`execute-v1-openapi\.json`"
)


def test_readme_schema_file_count_matches_the_shipped_directory() -> None:
    actual = len(list(SCHEMA_DIR.glob("*.json")))
    assert actual < len(WORDS), (
        f"{SCHEMA_DIR.name}/ holds {actual} JSON files, past the end of WORDS. "
        "Add the number word and update the README."
    )

    match = SCHEMA_COUNT_CLAIM.search(README.read_text(encoding="utf-8"))
    assert match is not None, (
        "The README no longer states how many JSON files ship under "
        f"{SCHEMA_DIR.name}/. Restore the claim or delete this test."
    )

    claimed = match.group(1).lower()
    assert claimed == WORDS[actual], (
        f"README says {claimed!r} JSON files ship under {SCHEMA_DIR.name}/, "
        f"but {actual} are there. Write {WORDS[actual]!r}."
    )


def test_readme_only_links_to_files_that_exist() -> None:
    text = README.read_text(encoding="utf-8")
    targets = re.findall(r"\]\((?!https?:)([^)#]+)", text)
    missing = [t for t in targets if not (ROOT / t).exists()]
    assert not missing, f"README links to paths that do not exist: {missing}"


def test_readme_names_only_schemas_that_ship() -> None:
    text = README.read_text(encoding="utf-8")
    named = set(re.findall(r"`([a-z0-9-]+\.json)`", text))
    shipped = {path.name for path in SCHEMA_DIR.glob("*.json")}
    missing = sorted(named - shipped)
    assert not missing, f"README names schema files that do not ship: {missing}"
