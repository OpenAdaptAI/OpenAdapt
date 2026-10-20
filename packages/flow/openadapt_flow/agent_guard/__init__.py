"""Guard an agent you already run.

A developer whose agent already drives a Chromium (Playwright MCP,
browser-use, a computer-use agent with a browser, their own Playwright
script) starts that browser with a DevTools port and runs
``openadapt-flow guard`` next to it. The agent doesn't change.

* **Run 1, guarded.** The guard attaches to the agent's tab over CDP, holds
  every save (:mod:`.requests`) until the record on screen is the one the task
  names (:mod:`.commit_guard`, over :mod:`openadapt_flow.guard`), blocks it
  and tells the agent why when it isn't, and reads the save back with
  ``verify-run``. ``observe_only`` never blocks.
* **Run 2, compiled.** A run that reads back correctly becomes a draft
  program (:mod:`.handoff`) with a record check built from the app's own
  reads. A person confirms the check and admits it; then it is an MCP tool
  that runs with no model calls (``openadapt-agent serve --teach``).

See ``docs/GUARD_AN_AGENT.md``.
"""

from openadapt_flow.agent_guard.commit_guard import CommitGuard, GuardSummary
from openadapt_flow.agent_guard.config import GuardConfig, load_config
from openadapt_flow.agent_guard.requests import HeldRequest, NotASave, classify

__all__ = [
    "CommitGuard",
    "GuardConfig",
    "GuardSummary",
    "HeldRequest",
    "NotASave",
    "classify",
    "load_config",
]
