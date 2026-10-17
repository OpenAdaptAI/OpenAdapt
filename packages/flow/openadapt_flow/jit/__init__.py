"""Compile-and-cache wrapper around computer-use agents.

Run 1 of a task lets a computer-use agent explore while Flow records it, and
returns a DRAFT program. A named person reviews it, attends a canary run, and
admits it. Later runs execute the admitted program with no model calls and an
independently verified effect. See docs/JIT.md.
"""

from openadapt_flow.jit.adapters import (
    DIALECTS,
    CaptureComputer,
    Observation,
    UnsupportedAction,
    dialects,
    register_dialect,
    translate,
)
from openadapt_flow.jit.compile_agent import (
    RECORDING_WARNING,
    CompiledAgent,
    ComputerUseAgent,
    JitResult,
    Recording,
    TaskRequest,
    WebApp,
    compile_agent,
)
from openadapt_flow.jit.keys import (
    CustomerModelKey,
    MissingModelKey,
    load_customer_model_key,
)
from openadapt_flow.jit.lifecycle import AdmissionError, BundleState
from openadapt_flow.jit.signature import ParamError, TaskSignature

__all__ = [
    "DIALECTS",
    "RECORDING_WARNING",
    "AdmissionError",
    "BundleState",
    "CaptureComputer",
    "CompiledAgent",
    "ComputerUseAgent",
    "CustomerModelKey",
    "JitResult",
    "MissingModelKey",
    "Observation",
    "ParamError",
    "Recording",
    "TaskRequest",
    "TaskSignature",
    "UnsupportedAction",
    "WebApp",
    "compile_agent",
    "dialects",
    "load_customer_model_key",
    "register_dialect",
    "translate",
]
