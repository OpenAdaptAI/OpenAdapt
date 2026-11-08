"""Synthetic source probes for the standalone CLI constructor inventory."""

from __future__ import annotations

import ast
import runpy
from pathlib import Path

import pytest

# Each form uses the same policy matrix and the same existing inventory entry
# point. None of these sources is imported or run as an application.
FORMS = (
    ("qualified", "import argparse", "argparse.ArgumentParser"),
    ("module_alias", "import argparse as ap", "ap.ArgumentParser"),
    ("direct", "from argparse import ArgumentParser", "ArgumentParser"),
    (
        "renamed",
        "from argparse import ArgumentParser as Parser",
        "Parser",
    ),
    (
        "qualified_subclass",
        "import argparse\nclass Parser(argparse.ArgumentParser): pass",
        "Parser",
    ),
    (
        "aliased_transitive_subclass",
        "import argparse as ap\nclass Base(ap.ArgumentParser): pass\n"
        "class Parser(Base): pass",
        "Parser",
    ),
    (
        "direct_subclass",
        "from argparse import ArgumentParser as Base\nclass Parser(Base): pass",
        "Parser",
    ),
    (
        "assignment_alias",
        "import argparse as ap\nModule = ap\nParser = Module.ArgumentParser",
        "Parser",
    ),
    (
        "ambiguous_module_rebinding",
        "import argparse as ap\nif condition: ap = unrelated",
        "ap.ArgumentParser",
    ),
    (
        "ambiguous_base_rebinding",
        "from argparse import ArgumentParser as Base\nAlias = Base\n"
        "if condition: Alias = unrelated\nclass Parser(Alias): pass",
        "Parser",
    ),
    (
        "conditional_expression_alias",
        "import argparse as ap\nParser = ap.ArgumentParser if condition else unrelated",
        "Parser",
    ),
    (
        "list_walrus_alias",
        "import argparse as ap",
        "def build():\n unused = [(Parser := ap.ArgumentParser) for _ in (1,)]\n"
        " return Parser({settings})",
    ),
    (
        "set_walrus_alias",
        "import argparse as ap",
        "def build():\n unused = {{(Parser := ap.ArgumentParser) for _ in (1,)}}\n"
        " return Parser({settings})",
    ),
    (
        "dict_walrus_alias",
        "import argparse as ap",
        "def build():\n unused = {{_: (Parser := ap.ArgumentParser) for _ in (1,)}}\n"
        " return Parser({settings})",
    ),
    (
        "generator_walrus_alias",
        "import argparse as ap",
        "def build():\n unused = list((Parser := ap.ArgumentParser) for _ in (1,))\n"
        " return Parser({settings})",
    ),
    (
        "list_walrus_free_alias",
        "import argparse as ap",
        "def build():\n Parser = ap.ArgumentParser\n"
        " return [Parser({settings}) for _ in (1,) if (Parser := Parser)]",
    ),
    (
        "set_walrus_free_alias",
        "import argparse as ap",
        "def build():\n Parser = ap.ArgumentParser\n"
        " return {{Parser({settings}) for _ in (1,) if (Parser := Parser)}}",
    ),
    (
        "dict_walrus_free_alias",
        "import argparse as ap",
        "def build():\n Parser = ap.ArgumentParser\n"
        " return {{_: Parser({settings}) for _ in (1,) if (Parser := Parser)}}",
    ),
    (
        "generator_walrus_free_alias",
        "import argparse as ap",
        "def build():\n Parser = ap.ArgumentParser\n"
        " return list(Parser({settings}) for _ in (1,) if (Parser := Parser))",
    ),
    ("subparser", "", 'subparsers.add_parser("synthetic", {settings})'),
)
SETTINGS = (
    pytest.param("allow_abbrev=False", True, id="false"),
    pytest.param("", False, id="missing"),
    pytest.param("allow_abbrev=True", False, id="true"),
    pytest.param("allow_abbrev=setting", False, id="dynamic"),
)


def _source(form, settings):
    _, prefix, constructor = form
    call = (
        constructor.format(settings=settings)
        if "{settings}" in constructor
        else f"{constructor}({settings})"
    )
    return prefix + "\n" + call + "\n"


@pytest.fixture
def inventory_module():
    # Loading only this test module does not call its runtime-parser tests.
    return runpy.run_path(str(Path(__file__).with_name("test_cli_no_abbreviations.py")))


@pytest.fixture
def synthetic_inventory(tmp_path, monkeypatch, inventory_module):
    inventory = inventory_module[
        "test_standalone_cli_constructor_inventory_also_disables_abbreviations"
    ]
    for relative in ("packages/flow", "openadapt", "packages/agent", "scripts"):
        (tmp_path / relative).mkdir(parents=True)
    location = tmp_path / "packages/flow/tests/test_inventory.py"
    monkeypatch.setitem(inventory.__globals__, "__file__", str(location))
    baseline = (
        "import argparse\n" + "argparse.ArgumentParser(allow_abbrev=False)\n" * 501
    )
    (tmp_path / "scripts/canonical.py").write_text(baseline, encoding="utf-8")

    def check(source, relative="scripts/synthetic.py"):
        (tmp_path / relative).write_text(source, encoding="utf-8")
        inventory()

    return check


@pytest.mark.parametrize("form", FORMS, ids=[form[0] for form in FORMS])
@pytest.mark.parametrize(("settings", "strict"), SETTINGS)
def test_constructor_forms_require_literal_false(
    synthetic_inventory, form, settings, strict
):
    source = _source(form, settings)
    if strict:
        synthetic_inventory(source)
    else:
        with pytest.raises(AssertionError, match="synthetic.py"):
            synthetic_inventory(source)


@pytest.mark.parametrize("form", FORMS, ids=[form[0] for form in FORMS])
def test_every_constructor_form_is_counted(inventory_module, form):
    calls = list(
        inventory_module["_parser_constructor_calls"](
            ast.parse(_source(form, "allow_abbrev=False"))
        )
    )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "relative",
    (
        "packages/flow/synthetic.py",
        "openadapt/synthetic.py",
        "packages/agent/synthetic.py",
        "scripts/synthetic.py",
    ),
)
def test_every_existing_inventory_root_remains_checked(synthetic_inventory, relative):
    with pytest.raises(AssertionError, match="synthetic.py"):
        synthetic_inventory("from argparse import ArgumentParser as P\nP()", relative)


def test_constructor_count_guard_remains(synthetic_inventory):
    with pytest.raises(AssertionError):
        synthetic_inventory("", "scripts/canonical.py")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "def build():\n import argparse as ap\n ap.ArgumentParser()\n"
            "ap.ArgumentParser()\n",
            1,
        ),
        (
            "import argparse as ap\ndef build(ap):\n ap.ArgumentParser()\n"
            "ap.ArgumentParser()\n",
            1,
        ),
        (
            "def build():\n return Parser()\nimport argparse as ap\n"
            "class Base(ap.ArgumentParser): pass\nclass Parser(Base): pass\n",
            1,
        ),
        (
            "def build():\n from argparse import ArgumentParser as Base\n"
            " class Parser(Base): pass\n return Parser()\nParser()\n",
            1,
        ),
        (
            "import argparse as ap\ndef build():\n ap = unrelated\n ap.ArgumentParser()\n",
            0,
        ),
        (
            "from argparse import ArgumentParser as Parser\n"
            "def build():\n from unrelated import Parser\n Parser()\n",
            0,
        ),
        (
            "import argparse as ap\nclass Namespace:\n"
            " from argparse import ArgumentParser as Parser\n"
            " def build(self):\n  return Parser()\n",
            0,
        ),
        (
            "import argparse as ap\nif condition:\n ap = unrelated\n"
            "ap.ArgumentParser()\n",
            1,
        ),
        (
            "from argparse import ArgumentParser as Base\nAlias = Base\n"
            "if condition:\n Alias = unrelated\nclass Parser(Alias): pass\nParser()\n",
            1,
        ),
        (
            "import argparse as ap\ndef build():\n global ap\n"
            " ap = unrelated\n return ap.ArgumentParser()\n",
            1,
        ),
        (
            "def outer():\n import argparse as ap\n def inner():\n"
            "  nonlocal ap\n  ap = unrelated\n  return ap.ArgumentParser()\n",
            1,
        ),
        (
            "from unrelated import ArgumentParser as Parser\nParser()\n",
            0,
        ),
        (
            "import unrelated as ap\nap.ArgumentParser()\n",
            0,
        ),
        (
            "import argparse as ap\nf = lambda ap: ap.ArgumentParser()\n"
            "g = lambda: ap.ArgumentParser()\n",
            1,
        ),
    ],
)
def test_static_bindings_follow_lexical_scopes(inventory_module, source, expected):
    calls = list(inventory_module["_parser_constructor_calls"](ast.parse(source)))
    assert len(calls) == expected


@pytest.mark.parametrize(
    "expression",
    (
        "[{body} for ap in {iterable}]",
        "{{{body} for ap in {iterable}}}",
        "{{ap: {body} for ap in {iterable}}}",
        "({body} for ap in {iterable})",
    ),
    ids=("list", "set", "dict", "generator"),
)
@pytest.mark.parametrize(
    ("body", "iterable", "expected"),
    (
        ("ap", "()", 1),
        ("ap.ArgumentParser()", "()", 1),
        ("ap", "ap.ArgumentParser()", 2),
    ),
    ids=("target-does-not-leak", "target-shadows-body", "first-iterable-is-outer"),
)
def test_comprehensions_keep_their_own_bindings(
    inventory_module, expression, body, iterable, expected
):
    comprehension = expression.format(body=body, iterable=iterable)
    source = (
        "import argparse as ap\ndef build():\n"
        f" unused = {comprehension}\n return ap.ArgumentParser()\n"
    )
    calls = list(inventory_module["_parser_constructor_calls"](ast.parse(source)))
    assert len(calls) == expected


@pytest.mark.parametrize(
    "expression",
    (
        "[(Parser := ap.ArgumentParser) for ap in ()]",
        "{(Parser := ap.ArgumentParser) for ap in ()}",
        "{ap: (Parser := ap.ArgumentParser) for ap in ()}",
        "((Parser := ap.ArgumentParser) for ap in ())",
    ),
    ids=("list", "set", "dict", "generator"),
)
def test_comprehension_walrus_uses_its_value_scope(inventory_module, expression):
    source = (
        "import argparse as ap\ndef build():\n"
        f" unused = {expression}\n return Parser()\n"
    )
    calls = list(inventory_module["_parser_constructor_calls"](ast.parse(source)))
    assert calls == []
