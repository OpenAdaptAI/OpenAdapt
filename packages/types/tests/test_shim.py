"""openadapt-types is a shim over openadapt_flow.contracts: same objects, same names."""

import importlib
import importlib.resources
import subprocess
import sys

import openadapt_flow.contracts as contracts


def test_top_level_is_the_contracts_package() -> None:
    import openadapt_types

    assert openadapt_types is contracts


def test_submodules_are_the_same_objects() -> None:
    from openadapt_types import execute as old_execute
    from openadapt_types.execute import ExecuteTerminalOutcomeV1

    import openadapt_flow.contracts.execute as new_execute

    assert old_execute is new_execute
    assert ExecuteTerminalOutcomeV1 is new_execute.ExecuteTerminalOutcomeV1
    assert sys.modules["openadapt_types.execute"] is new_execute


def test_real_module_keeps_its_own_spec_and_resources() -> None:
    importlib.import_module("openadapt_types.vocabulary")
    vocab = sys.modules["openadapt_flow.contracts.vocabulary"]
    assert vocab.__spec__.name == "openadapt_flow.contracts.vocabulary"
    assert (importlib.resources.files("openadapt_flow.contracts") / "py.typed").is_file()


def test_unknown_submodule_still_raises() -> None:
    try:
        importlib.import_module("openadapt_types.does_not_exist")
    except ModuleNotFoundError:
        return
    raise AssertionError("expected ModuleNotFoundError")


def test_vocab_lint_command_target_resolves() -> None:
    from openadapt_types.vocabulary.lint import main

    from openadapt_flow.contracts.vocabulary.lint import main as new_main

    assert main is new_main
    result = subprocess.run(
        [sys.executable, "-c", "import openadapt_types, openadapt_flow.contracts as c; assert openadapt_types is c"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
