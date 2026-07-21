"""Scaffolding for the external, paid baseline arms — adapters, NOT live calls.

These arms are the point of the benchmark: measure the Silent Wrong-Effect Rate
of the leading computer-use agents against the SAME tasks + oracle the in-repo
arms run. But driving them costs money and needs credentials, so this module
ships only the **adapter scaffolding** — the interface each arm satisfies, a
precise docstring of how it MUST drive the shared substrate, and ``TODO``
markers where the live integration goes. Nothing here imports a paid SDK or
makes a network call; every :meth:`run` raises :class:`ScaffoldNotWired` until a
separately funded run supplies credentials and a budget cap.

Hard rules every live implementation MUST honor (enforced by review, mirrored
here as docstring contracts):

1. **Goal only, never steps.** Feed the arm ``task.goal`` (natural-language
   intent) and the run ``params`` it must write. NEVER pass a step list, the
   demonstration, or the expected-effect selector as instructions — that would
   leak the answer to a learning agent and void the comparison.
2. **Drive the SHARED substrate.** Act only through the ``session`` the harness
   provides (the same MockMed / EMR / RDP surface every arm sees). Do not open a
   side channel to the system of record; the independent oracle, not the agent,
   judges the effect.
3. **Record every model call.** Append one
   :class:`~openadapt_flow.benchmark.effectbench.schema.ModelCall` per API
   request with token counts and the pinned list price, so ``cost_usd`` is
   auditable ``sum(model_calls)``. An RPA tool with a flat run fee records a
   single ``ModelCall`` carrying that fee.
4. **Never spend without an explicit budget.** A live run must be gated on an
   opt-in flag AND a per-run + per-suite USD cap; a scaffolded arm must refuse.

To wire one later: implement ``_drive`` (see each class's TODOs), flip ``live``
to ``True`` behind the budget gate, and register it with the runner. Until then
the runner lists them as ``scaffolded`` and the dry-run skips them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from openadapt_flow.benchmark.effectbench.runner.arms import ArmResult
from openadapt_flow.benchmark.effectbench.runner.substrate import SubstrateSession
from openadapt_flow.benchmark.effectbench.schema import TaskSpec


class ScaffoldNotWired(NotImplementedError):
    """Raised when a scaffolded external baseline is run before it is wired.

    Carries the exact prerequisites (credentials, budget, opt-in flag) so a
    funded run knows what to supply. A subclass of :class:`NotImplementedError`
    so callers can catch either.
    """


@dataclass(frozen=True)
class BaselineRequirements:
    """What a live run of an external baseline must supply before it can run."""

    #: Provider / product name for logs and the run manifest.
    provider: str
    #: Environment variables / secrets the live client needs (names only).
    credentials: tuple[str, ...]
    #: Rough per-episode cost note (for budgeting; NOT a price the code trusts).
    est_cost_note: str
    #: The opt-in flag a funded run sets to authorize spend.
    optin_flag: str = "EFFECTBENCH_ALLOW_PAID_BASELINES"


class _ScaffoldArm:
    """Base for a not-yet-wired external baseline arm.

    Subclasses set :attr:`name`, :attr:`requires`, and document ``_drive`` in
    their class docstring. :meth:`run` refuses (never spends) with the concrete
    prerequisites, so the arm satisfies the ``AgentArm`` protocol and is listed
    as scaffolded without any risk of an accidental paid call.
    """

    #: Scaffolded arms are never live until their integration is implemented and
    #: placed behind the budget gate.
    live = False
    name = "scaffold"
    requires: BaselineRequirements = BaselineRequirements(
        provider="unknown", credentials=(), est_cost_note="unknown"
    )

    def run(
        self, task: TaskSpec, session: SubstrateSession, *, params: Mapping[str, str]
    ) -> ArmResult:
        raise ScaffoldNotWired(
            f"{self.name!r} is a scaffolded baseline, not wired for execution. "
            f"A funded run must: (1) set {self.requires.optin_flag}=1, "
            f"(2) provide credentials {list(self.requires.credentials)}, "
            f"(3) supply a per-run + per-suite USD budget cap, then implement "
            f"{type(self).__name__}._drive. Est. cost: {self.requires.est_cost_note}. "
            "No paid call is made from the scaffold."
        )

    # Subclasses implement this behind the budget gate. Signature is the wiring
    # contract: goal-only in, an untrusted report + recorded model calls out.
    def _drive(
        self, task: TaskSpec, session: SubstrateSession, *, params: Mapping[str, str]
    ) -> ArmResult:  # pragma: no cover - scaffold
        raise ScaffoldNotWired(f"{type(self).__name__}._drive not implemented")


class ClaudeComputerUseArm(_ScaffoldArm):
    """Anthropic Claude computer-use (the ``computer_use`` tool) — SCAFFOLD.

    Live ``_drive`` contract:

    * Load the pinned Anthropic model + ``computer-use`` beta tool. Seed the
      conversation with ``task.goal`` and the run ``params`` ONLY (never the
      demonstration or the effect selector).
    * Loop: send the screenshot the ``session`` renders, receive a tool action,
      apply it through the ``session`` action API, repeat until the model
      reports done or an action budget is hit. Honor the MEMORY note in this
      repo: a ``screenshot``/``wait`` tool result must be returned as a
      ``tool_result``, never as a terminal "done".
    * Set ``AgentReport.reported_success`` from the model's final assertion /
      the on-screen state (the untrusted witness the oracle will cross-check),
      and ``halted`` if it refused/stopped.
    * Append one :class:`ModelCall` per request (``input_tokens`` /
      ``output_tokens`` × the pinned list price) for auditable ``cost_usd``.

    TODO(funded-run): implement the tool loop against the pinned model id; add
    the per-run screenshot/action budget; gate on the opt-in flag + USD cap.
    """

    name = "claude_cu"
    requires = BaselineRequirements(
        provider="anthropic",
        credentials=("ANTHROPIC_API_KEY",),
        est_cost_note="~$0.05-0.30 / episode (multi-turn vision tool loop)",
    )


class OpenAIOperatorArm(_ScaffoldArm):
    """OpenAI operator / computer-using-agent (CUA) — SCAFFOLD.

    Live ``_drive`` contract:

    * Use the pinned CUA / ``computer_use_preview`` tool via the Responses API.
      Provide ``task.goal`` + ``params`` as the objective; NEVER a step list.
    * Loop over ``computer_call`` actions, applying each to the ``session`` and
      returning the resulting screenshot as the ``computer_call_output``, until
      the agent finishes or the action budget is hit.
    * ``reported_success`` from the agent's final message / on-screen state;
      ``halted`` on a refusal or safety stop.
    * Record one :class:`ModelCall` per turn for ``cost_usd``.

    TODO(funded-run): implement the CUA action loop; map its action schema onto
    the ``session`` action API; gate on the opt-in flag + USD cap.
    """

    name = "openai_cua"
    requires = BaselineRequirements(
        provider="openai",
        credentials=("OPENAI_API_KEY",),
        est_cost_note="~$0.05-0.30 / episode (multi-turn CUA loop)",
    )


class UITarsArm(_ScaffoldArm):
    """An open VLM computer-use agent — UI-TARS — SCAFFOLD.

    Live ``_drive`` contract:

    * Serve the pinned UI-TARS checkpoint (self-hosted GPU endpoint — see the
      repo's GPU CLI; this arm's "cost" is compute, recorded as a
      :class:`ModelCall` with the metered GPU-seconds valued at the pinned
      instance price, not a token price).
    * Same goal-only objective + screenshot→action loop as the hosted agents,
      applied through the ``session``; ``reported_success`` from the final
      assertion / screen; ``halted`` on refusal.

    TODO(funded-run): stand up the served checkpoint; implement the grounding →
    action loop; record GPU-second cost; gate on the opt-in flag + budget.
    """

    name = "ui_tars"
    requires = BaselineRequirements(
        provider="ui-tars (self-hosted VLM)",
        credentials=("UITARS_ENDPOINT_URL",),
        est_cost_note="GPU-seconds at the pinned instance price (self-hosted)",
    )


class SkyvernArm(_ScaffoldArm):
    """An RPA / browser-automation agent — Skyvern — SCAFFOLD.

    Live ``_drive`` contract:

    * Drive Skyvern against the ``session``'s browser surface with ``task.goal``
      as the objective (goal only). Skyvern plans + executes; collect its
      terminal status as ``reported_success`` (the untrusted witness) and
      ``halted`` if it aborted.
    * Record cost as a single flat-fee :class:`ModelCall` (Skyvern bills per run
      / per step) so the suite's cost accounting stays uniform.
    * IMPORTANT licensing note: Skyvern is run as an EXTERNAL application (its
      own process / container), never vendored into this MIT package — mirror
      the workspace's copyleft-boundary rule.

    TODO(funded-run): launch the external Skyvern runner; map its task API onto
    the ``session``; record the flat run fee; gate on the opt-in flag + budget.
    """

    name = "skyvern"
    requires = BaselineRequirements(
        provider="skyvern (external RPA app)",
        credentials=("SKYVERN_API_KEY", "SKYVERN_BASE_URL"),
        est_cost_note="per-run / per-step flat fee (recorded as one ModelCall)",
    )


#: Every scaffolded external baseline, for the runner's ``--list-arms`` and for
#: a funded run to enumerate and wire.
SCAFFOLDED_ARMS: tuple[_ScaffoldArm, ...] = (
    ClaudeComputerUseArm(),
    OpenAIOperatorArm(),
    UITarsArm(),
    SkyvernArm(),
)


__all__ = [
    "ScaffoldNotWired",
    "BaselineRequirements",
    "ClaudeComputerUseArm",
    "OpenAIOperatorArm",
    "UITarsArm",
    "SkyvernArm",
    "SCAFFOLDED_ARMS",
]
