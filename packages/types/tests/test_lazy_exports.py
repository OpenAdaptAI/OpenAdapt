"""``import openadapt_types`` is lazy and still exports every public name.

The package root loads no models until a name is used, so Flow (which imports
``openadapt_types.check_level`` at startup) and callers that need one contract
do not build every Pydantic model. Static analysis sees the same names through
the TYPE_CHECKING imports; these tests keep the three lists in step.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import openadapt_types

INIT = Path(openadapt_types.__file__)


def _type_checking_imports() -> dict[str, str]:
    tree = ast.parse(INIT.read_text(encoding="utf-8"))
    block = next(
        node
        for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
    )
    return {
        alias.name: node.module.removeprefix("openadapt_types.")
        for node in block.body
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }


def test_export_map_matches_type_checking_imports_and_all() -> None:
    assert openadapt_types._EXPORTS == _type_checking_imports()
    assert set(openadapt_types.__all__) == set(openadapt_types._EXPORTS)
    assert len(openadapt_types.__all__) == len(set(openadapt_types.__all__))


def test_every_public_name_resolves_to_its_submodule_object() -> None:
    for name, submodule in openadapt_types._EXPORTS.items():
        module = __import__(f"openadapt_types.{submodule}", fromlist=[name])
        assert getattr(openadapt_types, name) is getattr(module, name), name


def test_importing_the_package_or_check_level_loads_no_models() -> None:
    code = (
        "import sys\n"
        "import openadapt_types.check_level\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('openadapt_types'))\n"
        "assert loaded == ['openadapt_types', 'openadapt_types.check_level'], loaded\n"
        "assert 'pydantic' not in sys.modules\n"
        "from openadapt_types import Action\n"
        "assert Action.__module__ == 'openadapt_types.action'\n"
        "print('LAZY_OK')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "LAZY_OK" in result.stdout


def test_star_import_unknown_names_and_version() -> None:
    namespace: dict[str, object] = {}
    exec("from openadapt_types import *", namespace)
    assert set(openadapt_types.__all__) <= set(namespace)
    assert openadapt_types.__version__
    assert "Action" in dir(openadapt_types)
    try:
        openadapt_types.DoesNotExist  # noqa: B018
    except AttributeError:
        return
    raise AssertionError("expected AttributeError")
