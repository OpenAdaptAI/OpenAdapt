"""The runtime.core boundary stays complete as the package grows.

``lint-imports`` (CI, flow core lint) checks the contracts in
``.importlinter``. These tests keep the contracts themselves honest: every
governance module is on the forbidden list, and every action kind is carried
out by exactly one place.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

from openadapt_flow.ir import ActionKind
from openadapt_flow.runtime.core import ACTION_EXECUTORS

FLOW = Path(__file__).resolve().parents[1]
PACKAGE = FLOW / "openadapt_flow"

# Kinds the replayer still carries out itself (_act_without_dialog and the
# surface switch). Each moves into runtime.core with its own executor.
NOT_YET_IN_CORE = {
    ActionKind.CLICK,
    ActionKind.DOUBLE_CLICK,
    ActionKind.RIGHT_CLICK,
    ActionKind.DRAG,
    ActionKind.TYPE,
    ActionKind.SELECT_OPTION,
    ActionKind.SCROLL,
    ActionKind.READ,
    ActionKind.SWITCH_SURFACE,
}


def _contract(name: str) -> configparser.SectionProxy:
    config = configparser.ConfigParser()
    config.read(FLOW / ".importlinter")
    return config[f"importlinter:contract:{name}"]


def _modules(value: str) -> set[str]:
    return {line.strip() for line in value.splitlines() if line.strip()}


def test_every_qualification_module_is_forbidden_to_the_core() -> None:
    governance = {
        f"openadapt_flow.{path.stem if path.is_file() else path.name}"
        for path in PACKAGE.iterdir()
        if re.match(r"(production_)?qualif", path.name)
        and (path.suffix == ".py" or (path / "__init__.py").is_file())
    }
    for name in ("runtime-core-direct", "runtime-core-indirect"):
        forbidden = _modules(_contract(name)["forbidden_modules"])
        assert governance - forbidden == set(), name


def test_both_contracts_forbid_the_same_modules() -> None:
    direct = _modules(_contract("runtime-core-direct")["forbidden_modules"])
    indirect = _modules(_contract("runtime-core-indirect")["forbidden_modules"])
    assert direct == indirect


def test_every_action_kind_has_exactly_one_executor() -> None:
    in_core = set(ACTION_EXECUTORS)
    assert in_core & NOT_YET_IN_CORE == set()
    assert in_core | NOT_YET_IN_CORE == set(ActionKind)
