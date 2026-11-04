"""The engine-generality check: app names fail, examples and data pass."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_engine_generality.py"


@pytest.fixture(scope="module")
def check():
    spec = importlib.util.spec_from_file_location("check_engine_generality", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _engine(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def test_a_name_in_code_is_a_hit(check, tmp_path: Path) -> None:
    engine = _engine(tmp_path, {"runtime/x.py": 'if app == "OpenEMR":\n    pass\n'})
    assert check.scan(engine) == {"runtime/x.py": {"openemr": [1]}}


def test_an_identifier_without_a_name_is_a_hit(check, tmp_path: Path) -> None:
    engine = _engine(tmp_path, {"report.py": 'SECRET = {"token_main"}\n'})
    assert check.scan(engine) == {"report.py": {"token_main": [1]}}


def test_a_marked_example_comment_passes(check, tmp_path: Path) -> None:
    engine = _engine(
        tmp_path,
        {
            "a.py": "# example: OpenEMR keys rows by uuid\nx = 1\n",
            "d.py": "#: example: OpenEMR keys rows by uuid\nx = 1\n",
            # Without the marker the same comment is a hit.
            "b.py": "# OpenEMR keys rows by uuid\nx = 1\n",
            # The marker only counts on a comment line, not after code.
            "c.py": 'x = "OpenEMR"  # example: no\n',
        },
    )
    assert check.scan(engine) == {"b.py": {"openemr": [1]}, "c.py": {"openemr": [1]}}


def test_a_name_inside_an_identifier_is_a_hit(check, tmp_path: Path) -> None:
    engine = _engine(
        tmp_path,
        {
            "a.py": "def _openemr_fixup():\n    pass\n",
            "b.py": "APP_OPENEMR = 1\n",
            "c.py": "isOpenEMR = True\n",
            "d.py": "def to_stedi_body():\n    pass\n",
            "e.py": "EPIC_BASE = 1\n",
            "f.py": "row = 'tabLoanApplication'\n",
            # A name only stops at a letter or digit beside it.
            "g.py": "reopenemrs = 1\n",
            "h.py": "TextEdit = 1\nMockMedServer = 1\n",
            # Ordinary words that only look like a name.
            "i.py": "text_edit = 1\nmock_media = 1\n",
        },
    )
    assert check.scan(engine) == {
        "a.py": {"openemr": [1]},
        "b.py": {"openemr": [1]},
        "c.py": {"openemr": [1]},
        "d.py": {"stedi": [1]},
        "e.py": {"epic": [1]},
        "f.py": {"frappe-table": [1]},
        "h.py": {"mockmed": [2], "textedit": [1]},
    }


def test_the_marker_must_open_the_comment(check, tmp_path: Path) -> None:
    engine = _engine(tmp_path, {"a.py": "# OpenEMR-only hack # example: no\n"})
    assert check.scan(engine) == {"a.py": {"openemr": [1]}}


def test_app_profiles_are_data(check, tmp_path: Path) -> None:
    engine = _engine(tmp_path, {"app_profiles/openemr.yaml": "app: openemr\n"})
    assert check.scan(engine) == {}


def test_the_demo_app_is_named_only_in_demo_modules(check, tmp_path: Path) -> None:
    engine = _engine(
        tmp_path,
        {
            "tutorial.py": 'URL = "/api/db"  # the MockMed demo\n',
            "runtime/effects/rest.py": 'records_path = "/api/db"\n',
            # The demo modules may name the demo app, not a customer app.
            "demo_proof.py": "# OpenEMR\n",
        },
    )
    assert check.scan(engine) == {
        "runtime/effects/rest.py": {"mockmed-records": [1]},
        "demo_proof.py": {"openemr": [1]},
    }


def test_epic_matches_the_product_not_the_word(check, tmp_path: Path) -> None:
    engine = _engine(
        tmp_path, {"a.py": "# an epic refactor\n", "b.py": "# Epic sends\n"}
    )
    assert check.scan(engine) == {"b.py": {"epic": [1]}}


def test_the_baseline_only_shrinks(check) -> None:
    hits = {"a.py": {"openemr": [3, 9]}, "b.py": {"medplum": [1]}}
    baseline = {"a.py": {"openemr": 2}, "c.py": {"redmine": 1}}

    new, stale = check.compare(hits, baseline)

    assert len(new) == 1 and "b.py" in new[0] and "medplum" in new[0]
    assert len(stale) == 1 and "c.py" in stale[0]


def test_the_engine_passes_against_its_baseline(check) -> None:
    baseline = json.loads(check.BASELINE.read_text(encoding="utf-8"))
    new, stale = check.compare(check.scan(), baseline)
    assert not new, "\n".join(new)
    assert not stale, "\n".join(stale)
