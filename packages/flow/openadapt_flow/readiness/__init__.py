"""Shadow mode with a replay-to-scorecard.

A person does 20 to 50 real tasks while their browser recording captures
the network (``readiness import``). The candidate agent, or a compiled
program, then does the same tasks on a tab the shadow hold guards
(``readiness run``): it navigates, fills every field and is stopped before
Save, Submit or Send. Its proposed write is compared field by field with
what the person saved (``readiness compare``), and a one-page Readiness
Scorecard is written (``readiness report``).

See ``docs/READINESS.md``.
"""

from openadapt_flow.readiness.compare import compare_case, compare_runs, values_equal
from openadapt_flow.readiness.launcher import (
    AgentLauncher,
    LaunchJob,
    ProgramLauncher,
    SubprocessLauncher,
)
from openadapt_flow.readiness.reference import import_episodes, load_episodes
from openadapt_flow.readiness.runner import SCOPE_STATEMENT, RunnerConfig, run_episodes
from openadapt_flow.readiness.scorecard import build_scorecard, write_scorecard

__all__ = [
    "SCOPE_STATEMENT",
    "AgentLauncher",
    "LaunchJob",
    "ProgramLauncher",
    "RunnerConfig",
    "SubprocessLauncher",
    "build_scorecard",
    "compare_case",
    "compare_runs",
    "import_episodes",
    "load_episodes",
    "run_episodes",
    "values_equal",
    "write_scorecard",
]
