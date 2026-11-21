"""scripts/check_verdict_folds.py: each fold kind is counted, a registered
site is exempt, and only growth fails."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from scripts import check_verdict_folds as folds

CONFIG = folds.load_config()

SNIPPETS = {
    "says_success": 'def f():\n    return "VERIFIED"\n',
    "cmp_literal": 'def f(x):\n    return x == "ROLLED_BACK"\n',
    "bool_fold": "def f(results):\n    return all(r.ok for r in results)\n",
    "swallow": "def f(g):\n    try:\n        return g()\n    except Exception:\n        return 0\n",
    "slice_count": "def f(lines):\n    n = sum(1 for x in lines[:256] if x)\n    return n\n",
    "else_success": 'def f(x):\n    if x:\n        y = "REFUTED"\n    else:\n        y = "CONFIRMED"\n    return y\n',
}


def _count(tmp_path: Path, source: str) -> dict[str, int]:
    path = tmp_path / "m.py"
    path.write_text(textwrap.dedent(source))
    return dict(folds.count_file(path, CONFIG))


@pytest.mark.parametrize("kind", sorted(SNIPPETS))
def test_each_kind_is_counted(tmp_path: Path, kind: str) -> None:
    assert _count(tmp_path, SNIPPETS[kind]).get(kind, 0) >= 1


def test_closed_except_is_not_a_swallow(tmp_path: Path) -> None:
    src = "def f(g):\n    try:\n        return g()\n    except Exception as e:\n        return Errored(type(e).__name__, '')\n"
    assert "swallow" not in _count(tmp_path, src)


def test_a_registered_site_is_exempt(tmp_path: Path) -> None:
    body = "".join(SNIPPETS.values()).replace("def f(", "def g(")
    src = "@verdict_site('x', classes='C2')\n" + body.replace(
        "\ndef g(",
        "\n@verdict_site('x', classes='C2')\ndef g(",
    )
    assert _count(tmp_path, src) == {}


def test_only_growth_fails() -> None:
    base = {"a.py": {"swallow": 2}}
    assert folds.compare({"a.py": {"swallow": 3}}, base)
    assert folds.compare({"b.py": {"swallow": 1}}, base)
    assert not folds.compare({"a.py": {"swallow": 1}}, base)
    assert not folds.compare({}, base)


def test_the_baseline_only_lowers() -> None:
    base = {"a.py": {"swallow": 2, "bool_fold": 1}}
    assert folds.lowered({"a.py": {"swallow": 5}, "b.py": {"swallow": 1}}, base) == {
        "a.py": {"swallow": 2}
    }


def test_main_is_within_the_baseline() -> None:
    import json

    baseline = json.loads(folds.BASELINE.read_text(encoding="utf-8"))
    assert folds.compare(folds.scan(CONFIG), baseline) == []


def test_the_config_names_no_application() -> None:
    from scripts import check_engine_generality as gen

    text = folds.CONFIG.read_text(encoding="utf-8")
    for term, (pattern, _is_name) in gen._term_patterns(gen.load_config()).items():
        assert not pattern.search(text), term
