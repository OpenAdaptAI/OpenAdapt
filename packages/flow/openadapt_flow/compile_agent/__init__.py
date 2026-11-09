"""Compile a computer-use agent's first verified run into a program.

Run 1 lets a computer-use agent do the task in a harness that gives it the
page's structure first and screenshots only on request or at a gate, and that
checks the right record is on screen before every save. If the system of
record shows exactly the record the task named, the run is compiled, a person
confirms the proposed record checks, and the program is admitted under the
JIT lifecycle. Every later run makes no model calls. See docs/COMPILE_AGENT.md.

``ClaudeAgent(vision="code")`` acts through code instead: one terminal-style
tool, structured steps, a sandboxed shell. See docs/CODE_AS_ACTION.md.
"""

from openadapt_flow.compile_agent.agents import (
    AgentLedger,
    BudgetExceeded,
    ClaudeAgent,
    ScriptedAgent,
)
from openadapt_flow.compile_agent.code_action import (
    CodeActionTool,
    LeftTheApp,
    ScriptedCodeAgent,
    ScriptError,
    check_trace_against_ir,
)
from openadapt_flow.compile_agent.harness import (
    GateRefused,
    GateRequest,
    Harness,
    SaveGate,
)
from openadapt_flow.compile_agent.pipeline import (
    CompileResult,
    FirstRunCompiler,
    RecordCheck,
    ReplayResult,
    check_run_record,
    compile_first_run,
    run_compiled,
)

__all__ = [
    "AgentLedger",
    "BudgetExceeded",
    "ClaudeAgent",
    "CodeActionTool",
    "CompileResult",
    "FirstRunCompiler",
    "GateRefused",
    "GateRequest",
    "Harness",
    "LeftTheApp",
    "RecordCheck",
    "ReplayResult",
    "SaveGate",
    "ScriptError",
    "ScriptedAgent",
    "ScriptedCodeAgent",
    "check_run_record",
    "check_trace_against_ir",
    "compile_first_run",
    "run_compiled",
]
