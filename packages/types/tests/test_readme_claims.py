"""The README states facts about the package. Keep them true.

A count written by hand goes stale the moment a schema is added or removed,
and a reader has no way to tell. These tests read the claim out of the README
and check it against the shipped tree. They also run every Python example in
the README, because PyPI shows the same page and a reader copies from it.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import pytest

from openadapt_types.vocabulary.lint import main as vocab_lint

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
CONTRACTS = ROOT / "docs" / "CONTRACTS.md"
def _resolve(target: str):
    """README paths under openadapt_types/ now live in openadapt_flow/contracts/."""
    if target.startswith("openadapt_types/"):
        return (ROOT.parent / "flow" / "openadapt_flow" / "contracts") / target[len("openadapt_types/") :]
    return ROOT / target


SCHEMA_DIR = (ROOT.parent / "flow" / "openadapt_flow" / "contracts") / "schemas"

# PyPI renders the README too, and it doesn't resolve relative links, so the
# README links to its own files by absolute GitHub URL.
REPO_FILE_URL = re.compile(
    r"\]\(https://github\.com/OpenAdaptAI/OpenAdapt/(?:blob|tree)/main/packages/types/([^)#]+)"
)
PYTHON_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)
# `print(...)  # expected output` on one line.
INLINE_OUTPUT = re.compile(r"^\s*print\(.*\)\s{2,}# (.*)$")

# Indexed by value, so WORDS[17] == "seventeen". Extend it when the directory
# grows past the end rather than dropping the count from the README.
WORDS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
    "twenty",
    "twenty-one",
    "twenty-two",
    "twenty-three",
    "twenty-four",
    "twenty-five",
    "twenty-six",
    "twenty-seven",
    "twenty-eight",
    "twenty-nine",
    "thirty",
    "thirty-one",
    "thirty-two",
    "thirty-three",
    "thirty-four",
    "thirty-five",
    "thirty-six",
    "thirty-seven",
    "thirty-eight",
    "thirty-nine",
    "forty",
    "forty-one",
    "forty-two",
    "forty-three",
    "forty-four",
    "forty-five",
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
    targets += REPO_FILE_URL.findall(text)
    missing = [t for t in targets if not _resolve(t).exists()]
    assert not missing, f"README links to paths that do not exist: {missing}"


def test_readme_uses_absolute_links_for_pypi() -> None:
    text = README.read_text(encoding="utf-8")
    relative = re.findall(r"\]\((?!https?:|#)([^)]+)", text)
    assert not relative, (
        f"PyPI can't resolve relative links in the README: {relative}. "
        "Link to https://github.com/OpenAdaptAI/OpenAdapt/blob/main/packages/types/<path>."
    )


def _expected_output(block: str) -> list[str]:
    """Return the output a README block says it prints.

    Two forms count: ``print(x)  # output`` on one line, and comment lines
    directly under a ``print`` call, one comment line per output line.
    """
    expected: list[str] = []
    after_print = False
    for line in block.splitlines():
        inline = INLINE_OUTPUT.match(line)
        if inline:
            expected.append(inline.group(1))
            after_print = False
        elif line.startswith("print("):
            after_print = True
        elif after_print and line.startswith("# "):
            expected.append(line[2:])
        else:
            after_print = False
    return expected


README_BLOCKS = PYTHON_BLOCK.findall(README.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "block", README_BLOCKS, ids=[f"block{i}" for i in range(len(README_BLOCKS))]
)
def test_readme_python_example_runs_and_prints_what_it_says(block: str) -> None:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exec(compile(block, "README.md", "exec"), {"__name__": "__readme__"})
    expected = _expected_output(block)
    if expected:
        assert stdout.getvalue().splitlines() == expected


def test_readme_has_python_examples() -> None:
    assert len(README_BLOCKS) >= 3


def test_readme_reads_plainly() -> None:
    # The README is the first page a developer sees on GitHub and PyPI. Terms
    # that only make sense inside OpenAdapt belong in docs/CONTRACTS.md.
    assert vocab_lint(["terms", "--level", "business", "--strict", str(README)]) == 0
    assert vocab_lint(["claims", "--strict", str(README)]) == 0


def test_contracts_doc_indexes_every_shipped_schema() -> None:
    text = CONTRACTS.read_text(encoding="utf-8")
    index = text.split("\n## ", 1)[0]
    named = set(re.findall(r"`([a-z0-9.-]+\.json)`", index))
    shipped = {path.name for path in SCHEMA_DIR.glob("*.json")}
    assert sorted(shipped - named) == [], "Add these schema files to the docs/CONTRACTS.md index"
    assert sorted(named - shipped) == [], "The docs/CONTRACTS.md index names files that don't ship"


def test_readme_names_only_schemas_that_ship() -> None:
    text = README.read_text(encoding="utf-8")
    named = set(re.findall(r"`([a-z0-9-]+\.json)`", text))
    shipped = {path.name for path in SCHEMA_DIR.glob("*.json")}
    missing = sorted(named - shipped)
    assert not missing, f"README names schema files that do not ship: {missing}"


def test_package_docstring_example_runs() -> None:
    # help(openadapt_types) shows this example, so it has to run as written.
    import textwrap

    import openadapt_types

    example = textwrap.dedent(openadapt_types.__doc__.split("Quick start::", 1)[1])
    exec(compile(example, "openadapt_types/__init__.py", "exec"), {})


# The README's Limits section describes current behavior. When a fix changes
# that behavior, these tests fail so the README stops describing a limit that
# is gone. Delete the README bullet and the test together.


def test_readme_limit_screenshot_bytes_still_do_not_serialize() -> None:
    from pydantic_core import PydanticSerializationError

    from openadapt_types import ComputerState

    assert "screenshot_png` holds raw bytes" in README.read_text(encoding="utf-8")
    with pytest.raises(PydanticSerializationError):
        ComputerState(screenshot_png=b"\x89PNG\r\n\x1a\n").model_dump_json()


def test_readme_limit_dsl_node_id_target_still_dropped() -> None:
    from openadapt_types import parse_action

    assert 'CLICK(node_id="n1")` returns a click with no target' in README.read_text(
        encoding="utf-8"
    )
    assert parse_action('CLICK(node_id="n1")').target is None
