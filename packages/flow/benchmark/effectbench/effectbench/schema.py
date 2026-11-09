"""EffectBench episode schema -- the data contract for one benchmark trial.

A benchmark run is a set of :class:`EpisodeRecord` rows; every headline metric
(SWER, over-halt, task success, the success-effect gap) is a pure function of
those rows (see :mod:`effectbench.metrics`). Nothing here executes an agent or
reads a system of record -- it only describes a task and records an outcome, so
the schema is import-light (pydantic only) and stable across substrates.

Outcome taxonomy (:class:`OutcomeLabel`) -- judged by an independent oracle
crossed with the agent's untrusted self-report, never by the screen alone:

- ``SUCCESS``             reported success AND exactly one correct effect landed.
- ``SAFE_HALT``           no effect; the correct action was NOT available.
- ``OVER_HALT``           no effect, but the correct action WAS available
                          (recoverable). Co-primary metric with SWER.
- ``SILENT_WRONG_EFFECT`` reported success while the record is wrong or empty.
                          The SWER numerator. Split into wrong_write vs phantom.
- ``FALSE_ABORT``         the correct effect landed but the run reported failure.
- ``WRONG_ACTION``        a wrong effect persisted AND the run reported failure.

Exactly ``SWER = |SILENT_WRONG_EFFECT| / N``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from effectbench.effect import Effect, EffectKind, Verdict


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Substrate(str, Enum):
    """The agent-facing substrate -- where the deception lives."""

    WEB = "web"  # DOM present but the agent runtime is pixel-only
    DESKTOP = "desktop"  # native Windows UIA / macOS AX / Linux AT-SPI
    REMOTE_DISPLAY = "remote_display"  # RDP / Citrix -- no DOM, pixels only


class DivergenceCategory(str, Enum):
    """The seven mechanisms by which a green screen hides a wrong record.

    C1-C5 are transactional fault classes; C6-C7 are wrong-record / silent-no-op
    classes. Every task declares exactly one primary category.
    """

    C1_PARTIAL_SAVE = "C1_partial_save"
    C2_DUPLICATE_SUBMISSION = "C2_duplicate_submission"
    C3_OPTIMISTIC_THEN_REJECT = "C3_optimistic_then_reject"
    C4_STALE_OVERWRITE = "C4_stale_overwrite"
    C5_DOUBLE_DELIVERED_INPUT = "C5_double_delivered_input"
    C6_WRONG_RECORD_HOMONYM = "C6_wrong_record_homonym"
    C7_SILENT_NOOP_WRONG_TARGET = "C7_silent_noop_wrong_target"
    CONTROL = "control"  # a clean / idempotent-fix control, not a fault


class OutcomeLabel(str, Enum):
    """The one label the oracle + self-report assign to an episode."""

    SUCCESS = "success"
    SAFE_HALT = "safe_halt"
    OVER_HALT = "over_halt"
    SILENT_WRONG_EFFECT = "silent_wrong_effect"
    FALSE_ABORT = "false_abort"
    WRONG_ACTION = "wrong_action"


class SwerVariant(str, Enum):
    """Which kind of silent wrong effect occurred."""

    WRONG_WRITE = "wrong_write"  # something wrong persisted (dup/partial/lost/wrong)
    PHANTOM = "phantom"  # nothing persisted behind a green report
    NONE = "none"


class OracleChannel(str, Enum):
    """The independent read path a task's oracle uses for the TRUE effect."""

    SQL = "sql"
    REST = "rest"
    FHIR = "fhir"
    FILE = "file"
    FS = "fs"
    SNAPSHOT = "snapshot"


class OracleSpec(BaseModel):
    """Declarative description of a task's independent effect oracle.

    The three boolean attestations are the non-gameability contract: the oracle
    channel is isolated from the agent's action channel, each trial writes a
    trial-unique payload, and the task ships refusal controls that must leave
    every row unchanged. A task is not release-eligible until
    ``adversarially_audited`` is True.
    """

    model_config = ConfigDict(extra="forbid")

    channel: OracleChannel
    description: str = ""
    #: Opaque, channel-specific wiring the runner uses to construct the concrete
    #: verifier. NEVER contains secrets.
    config: dict[str, Any] = Field(default_factory=dict)

    isolated_from_agent: bool = True
    trial_unique_payload: bool = True
    refusal_controls: bool = False
    adversarially_audited: bool = False


class TaskSpec(BaseModel):
    """One authored benchmark task.

    A task is substrate + category + a natural-language goal + an initial
    system-of-record state + the DECLARED expected effect + an
    :class:`OracleSpec`. Every arm receives the SAME ``goal`` (intent, never a
    step list) and is scored by the SAME oracle, so a difference between arms is
    the agent, not the harness.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    title: str = ""
    substrate: Substrate
    category: DivergenceCategory
    #: The natural-language intent handed identically to every agent (never a
    #: step list -- a fairness requirement).
    goal: str
    #: The DECLARED expected effect: what must be true of the system of record
    #: for this task to have actually succeeded.
    expected_effect: Effect
    oracle: OracleSpec
    #: The system-of-record state to seed before each trial.
    initial_state: dict[str, Any] = Field(default_factory=dict)

    reversible: bool = True
    effect_declared: bool = True
    #: Dev vs sequestered-test split. Test-split oracle configs / payloads are
    #: withheld from the public release.
    split: str = "dev"
    notes: str = ""

    @property
    def is_control(self) -> bool:
        return self.category is DivergenceCategory.CONTROL


class ModelCall(BaseModel):
    """One model invocation inside an episode (for cost / latency accounting)."""

    model_config = ConfigDict(extra="forbid")

    index: int
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    note: str = ""


class AgentReport(BaseModel):
    """The system-under-test's SELF-REPORT for one episode -- never trusted.

    ``reported_success`` is what the agent asserted or the screen rendered: the
    deceptive witness the independent oracle refuses to believe.
    """

    model_config = ConfigDict(extra="forbid")

    reported_success: bool
    halted: bool = False
    message: str = ""


class OracleVerdict(BaseModel):
    """The independent oracle's reading of the TRUE effect for one episode."""

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    kind: EffectKind
    channel: str = ""
    reason: str = ""
    observed_count: Optional[int] = None
    expected_count: Optional[int] = None
    before_reachable: bool = True
    effect_correct: bool = False
    effect_wrong_persisted: bool = False
    effect_absent: bool = False


class EpisodeRecord(BaseModel):
    """One trial of one task by one arm -- the atomic benchmark result row.

    Every headline metric is a pure aggregation over a list of these, so raw
    rows can be published for external replication.
    """

    model_config = ConfigDict(extra="forbid")

    episode_id: str
    task_id: str
    arm: str
    trial: int
    substrate: Substrate
    category: DivergenceCategory

    seed: Optional[int] = None
    expected_effect_hash: str = ""
    #: Whether the correct action was in fact AVAILABLE this episode -- the axis
    #: that separates OVER_HALT (available) from SAFE_HALT (not).
    correct_action_available: bool = True

    agent: AgentReport
    oracle: OracleVerdict
    outcome: OutcomeLabel
    swer_variant: SwerVariant = SwerVariant.NONE
    reason: str = ""

    model_calls: list[ModelCall] = Field(default_factory=list)
    cost_usd: float = 0.0
    latency_s: float = 0.0

    env_fingerprint: dict[str, Any] = Field(default_factory=dict)
    started_at: str = Field(default_factory=_utcnow)
    finished_at: str = Field(default_factory=_utcnow)

    @property
    def is_silent_wrong(self) -> bool:
        """The SWER numerator: reported success while the record was not correct."""
        return self.outcome is OutcomeLabel.SILENT_WRONG_EFFECT

    @property
    def is_over_halt(self) -> bool:
        return self.outcome is OutcomeLabel.OVER_HALT

    @property
    def is_effect_success(self) -> bool:
        """Effect-verified success -- the honest task-success numerator."""
        return self.outcome is OutcomeLabel.SUCCESS

    @property
    def reported_success(self) -> bool:
        """The screen-only / self-reported success the gap measures against."""
        return self.agent.reported_success
