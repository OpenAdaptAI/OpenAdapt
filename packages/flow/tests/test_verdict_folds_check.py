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
    "swallow_wide": "def f(g, log):\n    try:\n        g()\n    except Exception:\n        log.error('failed')\n    return True\n",
    "says_unchanged": 'def f():\n    return "no_change"\n',
    "empty_change_set": "def f(changes):\n    return not changes\n",
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


def test_each_exit_of_a_handler_is_judged(tmp_path: Path) -> None:
    mixed = "def f(g, x):\n    try:\n        return g()\n    except Exception:\n        if x:\n            raise\n        return True\n"
    assert _count(tmp_path, mixed).get("swallow") == 1
    literal = "def f(g):\n    try:\n        return g()\n    except Exception:\n        return 'not_checked'\n"
    assert "swallow" not in _count(tmp_path, literal)
    mixed_exp = "def f(g, ok):\n    try:\n        return g()\n    except Exception:\n        return True if ok else 'unknown'\n"
    assert _count(tmp_path, mixed_exp).get("swallow") == 1
    attr = "def f(g):\n    try:\n        return g()\n    except Exception:\n        return Verdict.INDETERMINATE\n"
    assert "swallow" not in _count(tmp_path, attr)
    reraise = "def f(g):\n    try:\n        return g()\n    except Exception:\n        raise\n"
    assert "swallow" not in _count(tmp_path, reraise)


@pytest.mark.parametrize(
    "handler",
    [
        "except Exception:\n        log.error('failed')",
        "except Exception as exc:\n        log.exception(exc)\n        x = 1",
        "except (ValueError, Exception):\n        return 0",
        "except builtins.Exception:\n        return 0",
        "except BaseException:\n        if x:\n            raise\n        log.error('x')",
        "except Exception:\n        for _ in x:\n            break",
        "except Exception:\n        log.error('unknown')",
        "except Exception:\n        log.warning(NOT_CHECKED)",
    ],
)
def test_every_open_way_out_of_a_broad_handler_is_counted(
    tmp_path: Path, handler: str
) -> None:
    src = f"def f(g, log, x):\n    try:\n        g()\n    {handler}\n    return True\n"
    got = _count(tmp_path, src)
    assert got.get("swallow", 0) + got.get("swallow_wide", 0) == 1, got


@pytest.mark.parametrize(
    "handler",
    [
        "except Exception:\n        log.error('failed')\n        raise",
        "except Exception:\n        status = 'not_checked'",
        "except Exception as exc:\n        out.append(Errored(type(exc).__name__, ''))",
        "except Exception:\n        if x:\n            raise\n        else:\n            return NOT_CHECKED",
        "except ValueError:\n        log.error('narrow')",
    ],
)
def test_a_closed_handler_is_not_counted(tmp_path: Path, handler: str) -> None:
    src = f"def f(g, log, x, out):\n    try:\n        g()\n    {handler}\n    return True\n"
    got = _count(tmp_path, src)
    assert "swallow" not in got and "swallow_wide" not in got, got


@pytest.mark.parametrize(
    "expr",
    [
        "not changes",
        "len(collateral) == 0",
        "deleted == []",
        "not any(c for c in diffs)",
        "frame.changes == set()",
    ],
)
def test_a_nothing_changed_test_is_counted(tmp_path: Path, expr: str) -> None:
    src = f"def f(changes, collateral, deleted, diffs, frame):\n    return {expr}\n"
    assert _count(tmp_path, src).get("empty_change_set") == 1


def test_a_file_that_does_not_parse_fails_the_check(tmp_path: Path) -> None:
    path = tmp_path / "m.py"
    path.write_text("def f(:\n")
    with pytest.raises(SyntaxError):
        folds.count_file(path, CONFIG)


def test_a_spec_id_is_an_id_argument_not_any_quoted_word() -> None:
    src = (
        "register(SiteSpec(id='a.judge', classes='C1', is_pass=lambda o: o == 'held'))\n"
        "register(make_spec(fn, 'b.judge', 'owned'), xfail='#1', known=['M1 P2: x'])\n"
    )
    assert folds.spec_ids(src) == {"a.judge", "b.judge"}


def test_a_site_id_on_two_functions_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "m.py"
    path.write_text(
        "@verdict_site('x', classes='C2')\ndef a(): ...\n@verdict_site('x', classes='C2')\ndef b(): ...\n"
    )
    with pytest.raises(ValueError):
        folds.decorated_sites(path)


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
    assert folds.compare({"a.py": {"swallow": 2}, "b.py": {"swallow": 1}}, base)
    assert not folds.compare({"a.py": {"swallow": 1}}, base)
    assert not folds.compare({}, base)


def test_moving_a_fold_to_a_new_module_is_not_growth() -> None:
    base = {"big.py": {"swallow": 5, "bool_fold": 1}}
    moved = {"big.py": {"swallow": 2}, "big_part.py": {"swallow": 3, "bool_fold": 1}}
    assert not folds.compare(moved, base)
    grown = folds.compare(
        {**moved, "big_part.py": {"swallow": 4, "bool_fold": 1}}, base
    )
    assert (
        len(grown) == 1
        and grown[0].startswith("swallow: 6")
        and "big_part.py 4" in grown[0]
    )


def test_the_baseline_only_lowers() -> None:
    base = {"a.py": {"swallow": 2, "bool_fold": 1}}
    assert folds.lowered({"a.py": {"swallow": 5}, "b.py": {"swallow": 1}}, base) == {
        "a.py": {"swallow": 2}
    }


def test_lowering_after_a_move_keeps_the_moved_fold() -> None:
    base = {"a.py": {"swallow": 2}}
    moved = {"a.py": {"swallow": 1}, "b.py": {"swallow": 1}}
    new = folds.lowered(moved, base)
    assert new == moved and not folds.compare(moved, new)


def test_every_decorated_site_has_a_spec_id() -> None:
    assert folds.unregistered_sites() == []


def test_a_conditional_site_is_found(tmp_path: Path) -> None:
    path = tmp_path / "m.py"
    path.write_text(
        "import sys\nif sys:\n    @verdict_site('x', classes='C2')\n    def a(): ...\n"
        "else:\n    @verdict_site('x', classes='C2')\n    def b(): ...\n"
    )
    with pytest.raises(ValueError):
        folds.decorated_sites(path)


def test_main_is_within_the_baseline() -> None:
    import json

    baseline = json.loads(folds.BASELINE.read_text(encoding="utf-8"))
    assert folds.compare(folds.scan(CONFIG), baseline) == []


def test_the_config_names_no_application() -> None:
    from scripts import check_engine_generality as gen

    text = folds.CONFIG.read_text(encoding="utf-8")
    for term, (pattern, _is_name) in gen._term_patterns(gen.load_config()).items():
        assert not pattern.search(text), term
