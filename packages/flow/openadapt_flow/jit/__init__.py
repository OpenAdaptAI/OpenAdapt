"""Compile-and-cache wrapper around computer-use agents.

Run 1 of a task lets a computer-use agent explore while Flow records it, and
returns a candidate program for a person to admit. Later runs execute the
admitted program with no model calls and an independently verified effect.
See docs/JIT.md.
"""

from openadapt_flow.jit.adapters import (
    DIALECTS,
    CaptureComputer,
    Observation,
    UnsupportedAction,
    translate,
)
from openadapt_flow.jit.compile_agent import (
    AdmissionError,
    CompiledAgent,
    ComputerUseAgent,
    JitResult,
    TaskRequest,
    WebApp,
    compile_agent,
)
from openadapt_flow.jit.signature import ParamError, TaskSignature

__all__ = [
    "DIALECTS",
    "AdmissionError",
    "CaptureComputer",
    "CompiledAgent",
    "ComputerUseAgent",
    "JitResult",
    "Observation",
    "ParamError",
    "TaskRequest",
    "TaskSignature",
    "UnsupportedAction",
    "WebApp",
    "compile_agent",
    "translate",
]
