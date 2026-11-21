"""openadapt-flow: record, compile, replay deterministically, heal on drift."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "3.4.0"

# Strict no-outbound mode follows the package into child interpreters, so a
# Python child that imports openadapt_flow is refused off-machine connections
# like its parent (docs/OFFLINE_SEALS.md). This stays eager: it runs before any
# other Flow code.
from openadapt_flow import no_outbound as _no_outbound

if TYPE_CHECKING:
    from openadapt_flow.ir import (  # noqa: F401
        ActionKind,
        Anchor,
        HealEvent,
        Landmark,
        Postcondition,
        PostconditionKind,
        Resolution,
        RunReport,
        Step,
        StepResult,
        UnarmedStep,
        Workflow,
    )

# Names re-exported from ``openadapt_flow.ir``. They load on first use, so
# importing a small module such as ``openadapt_flow.crypto`` no longer loads
# the whole schema and everything ``ir`` imports.
_IR_EXPORTS = frozenset(
    {
        "ActionKind",
        "Anchor",
        "HealEvent",
        "Landmark",
        "Postcondition",
        "PostconditionKind",
        "Resolution",
        "RunReport",
        "Step",
        "StepResult",
        "UnarmedStep",
        "Workflow",
    }
)


def __getattr__(name: str) -> Any:
    if name in _IR_EXPORTS:
        value = getattr(importlib.import_module("openadapt_flow.ir"), name)
        globals()[name] = value
        return value
    # Importing the package used to load ``ir`` and every module ``ir``
    # imports, so ``openadapt_flow.<submodule>`` worked without importing the
    # submodule first. Keep that working by importing it on first access.
    if not name.startswith("__"):
        try:
            return importlib.import_module(f"{__name__}.{name}")
        except ModuleNotFoundError as error:
            if error.name != f"{__name__}.{name}":
                raise
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _IR_EXPORTS)


_no_outbound.enforce_from_env()
