"""Flow must not import openadapt_types code its dependency floor lacks.

Flow 3.4.0 imported ``openadapt_types.receipt`` while its floor,
openadapt-types 0.24.0, had no such module, so a clean ``pip install`` failed
on every receipt path. These tests pin the release-consistency check that
catches that class (scripts/check_release_consistency.py).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import check_release_consistency as crc

ROOT = Path(__file__).resolve().parents[1]

FLOOR_SOURCES = {
    "openadapt_types": (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from openadapt_types.seal import canonical_json\n"
        "__all__ = ['canonical_json']\n"
    ),
    "openadapt_types.seal": (
        "def canonical_json(value):\n    return value\n"
        "class Seal:\n    pass\n"
        "KEY_ID = 'k'\n"
    ),
}


def _flow_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "packages" / "flow"
    for relative, source in files.items():
        path = root / "openadapt_flow" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return root


def _missing(tmp_path: Path, source: str) -> list[tuple[int, str, str | None]]:
    root = _flow_tree(tmp_path, {"module.py": source})
    found = crc.missing_types_references(crc.flow_types_references(root), FLOOR_SOURCES)
    return sorted({(item.line, item.module, item.name) for item in found})


def test_a_module_the_floor_lacks_is_reported_once_at_its_import(
    tmp_path: Path,
) -> None:
    # The 3.4.0 shape: a lazy import of a module the floor does not have,
    # then attributes read through it.
    source = (
        "def verify(data):\n"
        "    from openadapt_types import receipt as r\n"
        "    return r.loads_strict(data), r.RECEIPT_FILE\n"
        "def card():\n"
        "    from openadapt_types.exception_card import ExceptionCardV1\n"
        "    return ExceptionCardV1.model_validate({})\n"
    )
    assert _missing(tmp_path, source) == [
        (2, "openadapt_types", "receipt"),
        (5, "openadapt_types.exception_card", "ExceptionCardV1"),
    ]


def test_a_name_the_floor_module_lacks_is_reported(tmp_path: Path) -> None:
    source = (
        "from openadapt_types.seal import canonical_json, seal_key_id\n"
        "from openadapt_types import seal\n"
        "seal.KEY_ID\n"
        "seal.new_helper\n"
        "import openadapt_types.ledger\n"
    )
    assert _missing(tmp_path, source) == [
        (1, "openadapt_types.seal", "seal_key_id"),
        (4, "openadapt_types.seal", "new_helper"),
        (5, "openadapt_types.ledger", None),
    ]


def test_names_the_floor_has_pass(tmp_path: Path) -> None:
    source = (
        "import openadapt_types\n"
        "import openadapt_types.seal as s\n"
        "from openadapt_types import canonical_json, seal\n"
        "from openadapt_types.seal import Seal\n"
        "openadapt_types.seal\n"
        "s.canonical_json\n"
        "seal.Seal\n"
        "Seal.anything_on_the_class\n"
    )
    assert _missing(tmp_path, source) == []


def test_types_floor_reads_the_base_dependency(tmp_path: Path) -> None:
    root = tmp_path / "flow"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "openadapt-flow"\nversion = "9.0.0"\n'
        'dependencies = ["pydantic>=2", "openadapt-types>=0.30.2,<0.31.0"]\n',
        encoding="utf-8",
    )
    assert crc.types_floor(root) == "0.30.2"


@pytest.fixture
def no_remote_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    """These repositories have no origin: it has none of their tags."""
    monkeypatch.setattr(crc, "_remote_has_tag", lambda repo, ref: False)


def _monorepo(tmp_path: Path, *, floor: str, in_tree: str) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    flow = tmp_path / "packages" / "flow"
    types = tmp_path / "packages" / "types"
    (flow / "openadapt_flow").mkdir(parents=True)
    (types / "openadapt_types").mkdir(parents=True)
    (flow / "pyproject.toml").write_text(
        '[project]\nname = "openadapt-flow"\nversion = "9.0.0"\n'
        f'dependencies = ["openadapt-types>={floor},<1"]\n',
        encoding="utf-8",
    )
    (types / "pyproject.toml").write_text(
        f'[project]\nname = "openadapt-types"\nversion = "{in_tree}"\n',
        encoding="utf-8",
    )
    (types / "openadapt_types" / "__init__.py").write_text("", encoding="utf-8")
    (flow / "openadapt_flow" / "uses.py").write_text(
        "from openadapt_types.ledger import Row\n", encoding="utf-8"
    )
    return flow


def test_an_untagged_floor_must_be_the_in_tree_version(
    tmp_path: Path, no_remote_tags: None
) -> None:
    flow = _monorepo(tmp_path, floor="0.30.0", in_tree="0.29.0")
    with pytest.raises(ValueError, match="has no tag openadapt-types-v0.30.0"):
        crc.validate_types_floor_imports(flow)


def test_an_untagged_floor_checks_the_in_tree_sources(
    tmp_path: Path, no_remote_tags: None
) -> None:
    flow = _monorepo(tmp_path, floor="0.30.0", in_tree="0.30.0")
    with pytest.raises(ValueError, match=r"uses\.py:1: openadapt_types\.ledger\.Row"):
        crc.validate_types_floor_imports(flow)
    ledger = tmp_path / "packages" / "types" / "openadapt_types" / "ledger.py"
    ledger.write_text("class Row:\n    pass\n", encoding="utf-8")
    assert "unreleased 0.30.0" in crc.validate_types_floor_imports(flow)


def test_a_tagged_floor_is_read_from_the_tag_not_the_tree(
    tmp_path: Path, no_remote_tags: None
) -> None:
    flow = _monorepo(tmp_path, floor="0.30.0", in_tree="0.30.1")
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "floor"], check=True)
    subprocess.run([*git, "tag", "openadapt-types-v0.30.0"], check=True)
    # Added after the tag: the tree has it, the floor release does not.
    ledger = tmp_path / "packages" / "types" / "openadapt_types" / "ledger.py"
    ledger.write_text("class Row:\n    pass\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"openadapt-types-v0\.30\.0\) lacks"):
        crc.validate_types_floor_imports(flow)


def test_flow_imports_exist_in_its_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """This tree's Flow against its floor, without a network tag fetch."""
    floor = crc.types_floor(ROOT)
    tag = f"{crc.TYPES_TAG_PREFIX}{floor}"
    repo = ROOT.parents[1]
    if subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/tags/{tag}",
        ],
        capture_output=True,
    ).returncode:
        if crc._types_tree_version(repo) != floor:
            pytest.skip(
                f"{tag} is not in this checkout; flow-lint's release-consistency "
                "step fetches it and runs this check"
            )
        monkeypatch.setattr(crc, "_types_sources_at_tag", lambda repo, tag: None)
    crc.validate_types_floor_imports(ROOT)


def test_an_unanswered_tag_query_fails_closed(tmp_path: Path) -> None:
    # No origin: the check can't tell whether the floor is tagged, so it
    # stops instead of falling back to the in-tree sources.
    flow = _monorepo(tmp_path, floor="0.30.0", in_tree="0.30.0")
    with pytest.raises(ValueError, match="could not ask origin"):
        crc.validate_types_floor_imports(flow)


def test_a_failed_fetch_of_an_existing_tag_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = _monorepo(tmp_path, floor="0.30.0", in_tree="0.30.0")
    monkeypatch.setattr(crc, "_remote_has_tag", lambda repo, ref: True)
    with pytest.raises(ValueError, match="fetching it failed"):
        crc.validate_types_floor_imports(flow)
