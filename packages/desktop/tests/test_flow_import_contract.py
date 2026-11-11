"""Check every Desktop Flow import against the runtime selected by CI.

Source tests exercise the same checkout. Bundled qualification requires the
real exact-pinned wheel installed by Desktop's build extra.
"""

from __future__ import annotations

import ast
import importlib
import os
import tomllib
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def _references(source: str) -> set[tuple[str, tuple[str, ...]]]:
    references: set[tuple[str, tuple[str, ...]]] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            references.update(
                (alias.name, ())
                for alias in node.names
                if alias.name == "openadapt_flow" or alias.name.startswith("openadapt_flow.")
            )
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module == "openadapt_flow" or node.module.startswith("openadapt_flow."):
                assert node.level == 0, "Flow imports must be absolute"
                references.add((node.module, tuple(alias.name for alias in node.names)))
        elif isinstance(node, ast.Call) and node.args:
            function = node.func
            literal = node.args[0]
            dynamic = (isinstance(function, ast.Name) and function.id == "import_module") or (
                isinstance(function, ast.Attribute) and function.attr == "import_module"
            )
            if dynamic and isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                if literal.value == "openadapt_flow" or literal.value.startswith("openadapt_flow."):
                    references.add((literal.value, ()))
    return references


def _bundled_flow_files(*, required: bool) -> set[Path]:
    try:
        installed = distribution("openadapt-flow")
    except PackageNotFoundError:
        if required:
            pytest.fail("the exact bundled Flow wheel is required in this CI lane")
        pytest.skip("the bundled Flow wheel is installed in the qualification-contract CI job")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pins = [
        Requirement(raw)
        for raw in project["project"]["optional-dependencies"]["build"]
        if Requirement(raw).name == "openadapt-flow"
    ]
    assert len(pins) == 1 and len(pins[0].specifier) == 1
    pin = next(iter(pins[0].specifier))
    assert pin.operator == "==" and "*" not in pin.version
    assert installed.version == pin.version, "the installed Flow does not match Desktop's exact pin"
    files = {
        Path(str(installed.locate_file(path))).resolve()
        for path in installed.files or []
        if str(path).startswith("openadapt_flow/") and str(path).endswith(".py")
    }
    assert files, "install the actual pinned Flow wheel, not an editable sibling"
    return files


@pytest.fixture(scope="module")
def flow_files(source_flow_package):
    if source_flow_package is not None:
        files = {path.resolve() for path in source_flow_package.rglob("*.py")}
        assert files and all(path.is_relative_to(source_flow_package) for path in files)
        return files
    return _bundled_flow_files(
        required=os.environ.get("OPENADAPT_DESKTOP_REQUIRE_BUNDLED_FLOW") == "1"
    )


def _real_import(name: str, files: set[Path]):
    module = importlib.import_module(name)
    assert isinstance(module.__file__, str) and Path(module.__file__).resolve() in files, (
        f"{name} did not come from the selected Flow runtime"
    )
    return module


def test_every_desktop_flow_import_uses_the_real_pinned_module(flow_files) -> None:
    references: set[tuple[str, tuple[str, ...]]] = set()
    for path in sorted((ROOT / "openadapt_desktop").rglob("*.py")):
        references.update(_references(path.read_text(encoding="utf-8")))
    assert references, "the Desktop/Flow import contract must exercise actual imports"
    for name, imported in sorted(references):
        module = _real_import(name, flow_files)
        for member in imported:
            if member != "*" and not hasattr(module, member):
                _real_import(name + "." + member, flow_files)


def test_missing_flow_module_is_not_replaced_by_a_stub(flow_files) -> None:
    with pytest.raises(ModuleNotFoundError):
        _real_import("openadapt_flow._synthetic_missing_contract_module", flow_files)


def test_required_contract_refuses_a_missing_distribution(monkeypatch) -> None:
    def missing(_name):
        raise PackageNotFoundError("openadapt-flow")

    monkeypatch.setattr(__name__ + ".distribution", missing)
    with pytest.raises(pytest.fail.Exception, match="exact bundled Flow wheel is required"):
        _bundled_flow_files(required=True)


def test_required_contract_refuses_a_different_distribution_version(monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(__name__ + ".distribution", lambda _name: SimpleNamespace(version="0.0.0"))
    with pytest.raises(AssertionError, match="does not match Desktop's exact pin"):
        _bundled_flow_files(required=True)


def test_import_inventory_covers_static_and_literal_dynamic_forms() -> None:
    assert _references(
        "import openadapt_flow.ir as ir\n"
        "from openadapt_flow import sections\n"
        "from openadapt_flow.runtime.effects import Effect\n"
        "import_module('openadapt_flow.runner.hosted_adapter')\n"
        "importlib.import_module('openadapt_flow.policy')\n"
    ) == {
        ("openadapt_flow.ir", ()),
        ("openadapt_flow", ("sections",)),
        ("openadapt_flow.runtime.effects", ("Effect",)),
        ("openadapt_flow.runner.hosted_adapter", ()),
        ("openadapt_flow.policy", ()),
    }
