"""Crystallize: a computer-use agent's first verified run becomes a program.

Run 1 lets a computer-use agent do the task in a harness that gives it the
page's structure first and screenshots only on request or at a gate, and that
checks the right record is on screen before every save. If the system of
record shows exactly the record the task named, the run is compiled, a person
confirms the proposed record checks, and the program is admitted under the
JIT lifecycle. Every later run makes no model calls. See docs/CRYSTALLIZE.md.
"""

from openadapt_flow.crystallize.agents import (
    AgentLedger,
    BudgetExceeded,
    ClaudeAgent,
    ScriptedAgent,
)
from openadapt_flow.crystallize.harness import (
    GateRefused,
    GateRequest,
    Harness,
    SaveGate,
)
from openadapt_flow.crystallize.pipeline import (
    Crystallizer,
    CrystallizeResult,
    RecordCheck,
    ReplayResult,
    check_run_record,
    crystallize,
    run_crystallized,
)

__all__ = [
    "AgentLedger",
    "BudgetExceeded",
    "ClaudeAgent",
    "CrystallizeResult",
    "Crystallizer",
    "GateRefused",
    "GateRequest",
    "Harness",
    "RecordCheck",
    "ReplayResult",
    "SaveGate",
    "ScriptedAgent",
    "check_run_record",
    "crystallize",
    "run_crystallized",
]
