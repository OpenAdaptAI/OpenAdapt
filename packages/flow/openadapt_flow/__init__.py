"""openadapt-flow: record, compile, replay deterministically, heal on drift."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "3.4.0"

# Strict no-outbound mode follows the package into child interpreters, so a
# Python child that imports openadapt_flow is refused off-machine connections
# like its parent (docs/OFFLINE_SEALS.md). This stays eager: it runs before any
# other Flow code.
from openadapt_flow import no_outbound as _no_outbound

if TYPE_CHECKING:
    from openadapt_flow.ir import (
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


def __getattr__(name: str) -> Any:
    # The ir re-exports load on first use, so importing a small module such as
    # ``openadapt_flow.crypto`` no longer loads the whole schema and everything
    # ``ir`` imports. Each branch is a plain import, so the import-layer checker
    # (tests/test_import_layers.py) sees every edge.
    if name == "ActionKind":
        from openadapt_flow.ir import ActionKind

        return ActionKind
    if name == "Anchor":
        from openadapt_flow.ir import Anchor

        return Anchor
    if name == "HealEvent":
        from openadapt_flow.ir import HealEvent

        return HealEvent
    if name == "Landmark":
        from openadapt_flow.ir import Landmark

        return Landmark
    if name == "Postcondition":
        from openadapt_flow.ir import Postcondition

        return Postcondition
    if name == "PostconditionKind":
        from openadapt_flow.ir import PostconditionKind

        return PostconditionKind
    if name == "Resolution":
        from openadapt_flow.ir import Resolution

        return Resolution
    if name == "RunReport":
        from openadapt_flow.ir import RunReport

        return RunReport
    if name == "Step":
        from openadapt_flow.ir import Step

        return Step
    if name == "StepResult":
        from openadapt_flow.ir import StepResult

        return StepResult
    if name == "UnarmedStep":
        from openadapt_flow.ir import UnarmedStep

        return UnarmedStep
    if name == "Workflow":
        from openadapt_flow.ir import Workflow

        return Workflow
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
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
]

_no_outbound.enforce_from_env()
