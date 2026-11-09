"""openadapt_flow.contracts is an alias of openadapt_types: same objects, same names.

The contracts moved back to openadapt-types (the pydantic-only leaf package).
Code that imports them through Flow must keep getting the identical module
and class objects, so isinstance checks, pickles and monkeypatches still work.
"""

from __future__ import annotations

import importlib
import importlib.resources
import pickle
import subprocess
import sys

import openadapt_types


def test_top_level_is_the_types_package() -> None:
    import openadapt_flow.contracts as contracts

    assert contracts is openadapt_types
    from openadapt_flow import contracts as attribute

    assert attribute is openadapt_types


def test_submodules_are_the_same_objects() -> None:
    from openadapt_flow.contracts import execute as old_execute
    from openadapt_flow.contracts.execute import ExecuteTerminalOutcomeV1

    import openadapt_types.execute as new_execute

    assert old_execute is new_execute
    assert ExecuteTerminalOutcomeV1 is new_execute.ExecuteTerminalOutcomeV1
    assert sys.modules["openadapt_flow.contracts.execute"] is new_execute


def test_wire_values_and_pickles_are_unchanged() -> None:
    from openadapt_flow.contracts.execute import ExecuteTerminalOutcomeV1

    import openadapt_types.execute as new_execute

    member = next(iter(ExecuteTerminalOutcomeV1))
    assert pickle.loads(pickle.dumps(member)) is new_execute.ExecuteTerminalOutcomeV1(
        member.value
    )


def test_real_module_keeps_its_own_spec_and_resources() -> None:
    importlib.import_module("openadapt_flow.contracts.vocabulary")
    vocab = sys.modules["openadapt_types.vocabulary"]
    assert vocab.__spec__.name == "openadapt_types.vocabulary"
    assert (importlib.resources.files("openadapt_types") / "py.typed").is_file()


def test_unknown_submodule_still_raises() -> None:
    try:
        importlib.import_module("openadapt_flow.contracts.does_not_exist")
    except ModuleNotFoundError:
        return
    raise AssertionError("expected ModuleNotFoundError")


def test_check_level_is_reexported() -> None:
    import openadapt_flow.check_level as old
    import openadapt_types.check_level as new

    assert old.CheckLevel is new.CheckLevel
    assert old.weakest is new.weakest
    assert old.from_oracle_tier(2) is new.CheckLevel.SYSTEM_OF_RECORD


def test_old_module_entry_points_still_run() -> None:
    from openadapt_flow.contracts.vocabulary.lint import main

    from openadapt_types.vocabulary.lint import main as new_main

    assert main is new_main
    result = subprocess.run(
        [sys.executable, "-m", "openadapt_flow.contracts.vocabulary", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
