"""openadapt-flow: record, compile, replay deterministically, heal on drift."""

__version__ = "2.7.0"

# Strict no-outbound mode follows the package into child interpreters, so a
# Python child that imports openadapt_flow is refused off-machine connections
# like its parent (docs/OFFLINE_SEALS.md).
from openadapt_flow import no_outbound as _no_outbound
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

_no_outbound.enforce_from_env()
