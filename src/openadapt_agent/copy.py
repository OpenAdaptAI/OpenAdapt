"""Canonical public sentences this package repeats.

README, server.json, llms.txt, and the first-party skill frontmatter share
``IDENTITY_SENTENCE`` (100 characters or fewer, the MCP registry limit). Skill
bodies add ``SKILL_WHEN_TO_USE`` and ``OUTCOME_RULES``.
"""

from __future__ import annotations

IDENTITY_SENTENCE = (
    "Your AI agent decides what to enter. OpenAdapt enters it in the app and checks that it saved."
)

SKILL_WHEN_TO_USE = (
    "Use this when your agent has decided what to enter and the app has no usable "
    "API. Call list_workflows, then run_workflow with the workflow name, its inputs, "
    "and your own request_id."
)

OUTCOME_RULES = (
    "Only outcome done means the change was saved and checked. needs_review means "
    "it stopped before saving and a person decides, so don't start the same work "
    "again. not_sure_if_saved means a person must check the record, so never retry "
    "it. did_not_run means nothing was written: fix the problem and retry with the "
    "same request_id when safe_to_retry is true."
)
#: Older name for ``OUTCOME_RULES``.
SKILL_HONESTY = OUTCOME_RULES

SKILL_NAME = "openadapt-gui-write"

#: The first command once this version is on PyPI: the zero-flag sandbox.
FIRST_COMMAND = "claude mcp add openadapt -- uvx --python 3.12 openadapt-agent serve"
#: The same command from the GitHub source, for use before the release.
PREVIEW_COMMAND = (
    "claude mcp add openadapt -- uvx --python 3.12 --from "
    "git+https://github.com/OpenAdaptAI/openadapt-agent openadapt-agent serve"
)
#: Older name for ``FIRST_COMMAND``.
THREE_LINE_INSTALL = FIRST_COMMAND
