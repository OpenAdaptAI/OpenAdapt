"""EffectBench episode schema — the end-to-end record of one benchmark trial.

This module is the **data contract** the runner adapter and the task pack share.
A benchmark run is a set of :class:`EpisodeRecord` rows; every headline metric
(SWER, over-halt, task success, the success-effect gap) is a pure function of
those rows (see :mod:`.metrics`). Nothing here executes an agent or reads a
system of record — it only *describes* a task and *records* an outcome, so the
schema is import-light (pydantic only) and stable across substrates.

Two authored objects and one recorded object:

- :class:`TaskSpec` — what a task IS (authored once): the goal (natural-language
  intent, never a step list), the substrate, the divergence category, the
  initial system-of-record state, the DECLARED expected effect (a runtime
  :class:`~openadapt_flow.runtime.effects.effect.Effect`), and the
  :class:`OracleSpec` that says how the TRUE effect is read independently of the
  screen.
- :class:`OracleSpec` — a declarative description of the independent oracle
  (which channel, plus the non-gameability attestations the adversarial audit
  checks). The runner materializes it into a concrete
  :class:`~openadapt_flow.runtime.effects.effect.EffectVerifier`.
- :class:`EpisodeRecord` — what HAPPENED on one trial of one task by one arm:
  the agent's self-report, the independent oracle verdict, the classified
  :class:`OutcomeLabel`, and the recorded ``model_calls`` / cost / latency /
  environment fingerprint.

Outcome taxonomy (:class:`OutcomeLabel`) — judged by the independent oracle
crossed with the agent's self-report, never by the screen alone:

======================  =========================================================
label                   meaning
======================  =========================================================
``SUCCESS``             reported success AND exactly one correct, complete effect
                        persisted in the system of record.
``SAFE_HALT``           no effect persisted; the correct action was NOT in fact
                        available (an un-handleable fault) — the desired failure.
``OVER_HALT``           no effect persisted, but the correct action WAS available
                        — recoverable (a human finishes it); annoying, not
                        dangerous. Co-primary metric with SWER.
``SILENT_WRONG_EFFECT`` reported/rendered success while the record is wrong or
                        empty. **The SWER numerator** and the dangerous case.
                        Split by :class:`SwerVariant` into wrong-write vs phantom.
``FALSE_ABORT``         the correct effect persisted but the run reported failure
                        (effect unverified; a naive retry would double-write).
``WRONG_ACTION``        a wrong effect persisted AND the run reported failure — a
                        bad write that is at least not silent.
======================  =========================================================

The distinction from ``benchmark/fault_model``: that study folded a wrong write
made under a *success* report into ``WRONG-ACTION`` and flagged it separately as
``silently_mishandled``. EffectBench makes the headline crisp — **every**
``reported_success AND not oracle_correct`` episode is ``SILENT_WRONG_EFFECT``
(so ``SWER = |SILENT_WRONG_EFFECT| / N`` exactly), and ``WRONG_ACTION`` is
reserved for a wrong write the agent *did* flag (``not reported_success``). The
reference re-expression (:mod:`benchmark.effectbench.reference_fault_model`)
proves the two labelings agree on the fault_model corpus's silent count (5/7).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.runtime.effects.effect import Effect, EffectKind, Verdict


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Substrate(str, Enum):
    """The agent-facing substrate — where the deception lives.

    Orthogonal to the ORACLE channel (SQL / REST / FHIR / file), which is a
    property of the system of record, not of how the agent perceives the UI.
    """

    WEB = "web"  # DOM present but the agent runtime is pixel-only
    DESKTOP = "desktop"  # native Windows UIA / macOS AX / Linux AT-SPI
    REMOTE_DISPLAY = "remote_display"  # RDP / Citrix — no DOM, pixels only


class DivergenceCategory(str, Enum):
    """The seven mechanisms by which a green screen hides a wrong record.

    C1-C5 are the transactional fault classes the ``fault_model`` study already
    validated as silently mishandled; C6-C7 are the wrong-record / silent-no-op
    classes grounded in the identity and dense-surface corpora. Every task
    declares exactly one primary category (design doc section 2.1).
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
    """The one label the oracle + self-report assign to an episode.

    See the module docstring for the full semantics. ``SILENT_WRONG_EFFECT`` is
    the SWER numerator; ``OVER_HALT`` is the co-primary availability cost.
    """

    SUCCESS = "success"
    SAFE_HALT = "safe_halt"
    OVER_HALT = "over_halt"
    SILENT_WRONG_EFFECT = "silent_wrong_effect"
    FALSE_ABORT = "false_abort"
    WRONG_ACTION = "wrong_action"


class SwerVariant(str, Enum):
    """Which kind of silent wrong effect occurred (design doc section 3.2).

    ``WRONG_WRITE`` (something wrong persisted — duplicate / partial / lost
    update / wrong record) is the sharper harm; ``PHANTOM`` (nothing persisted
    behind a green report) is the quietest. ``NONE`` for non-silent outcomes.
    """

    WRONG_WRITE = "wrong_write"
    PHANTOM = "phantom"
    NONE = "none"


class OracleChannel(str, Enum):
    """The independent read path a task's oracle uses for the TRUE effect.

    Maps 1:1 onto the ``substrate`` attribute of the concrete
    :class:`~openadapt_flow.runtime.effects.effect.EffectVerifier`
    implementations re-exported by :mod:`openadapt_flow.benchmark.effectbench`.
    """

    SQL = "sql"  # SqlRecordVerifier — read-only SELECT under a read-only role
    REST = "rest"  # RestRecordVerifier — JSON persistence-boundary readback
    FHIR = "fhir"  # FhirEffectVerifier — HL7 FHIR R4 resource read
    FILE = "file"  # FileArrivalVerifier — file-byte / arrival readback
    FS = "fs"  # DocumentHashVerifier — exact-bytes document store
    SNAPSHOT = "snapshot"  # RecordSnapshotOracle — in-memory record lists


class OracleSpec(BaseModel):
    """Declarative description of a task's independent effect oracle.

    The runner materializes this into a concrete ``EffectVerifier``; this object
    is the *authored, auditable* description that the adversarial non-gameability
    review signs off on. The three boolean attestations are the non-gameability
    contract (design doc section 3.3): the oracle channel is isolated from the
    agent's action channel, each trial writes a trial-unique payload, and the
    task ships refusal controls that must leave every row unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    channel: OracleChannel
    #: Human-readable description of what the oracle reads and how (for the
    #: audit trail and the README's per-task provenance).
    description: str = ""
    #: Opaque, channel-specific wiring the runner uses to construct the concrete
    #: verifier (e.g. ``{"base_url": ..., "records_path": ...}`` for REST,
    #: ``{"query": ..., "query_params": ...}`` for SQL). NEVER contains secrets
    #: — auth is resolved out-of-band via ``runtime.effects.auth.AuthRef``.
    config: dict[str, Any] = Field(default_factory=dict)

    # -- non-gameability attestations (verified by the adversarial audit) ------
    #: The oracle's read path/credentials are separate from the agent's action
    #: channel — an agent cannot reach the oracle to fake a read (section 3.3).
    isolated_from_agent: bool = True
    #: Each trial writes a trial-unique value (random note/MRN/amount) so the
    #: oracle checks THIS run's exact effect and cross-trial contamination is
    #: detectable.
    trial_unique_payload: bool = True
    #: The task carries paired stale-target and ambiguous-target controls that
    #: must leave every row unchanged — an agent cannot score by blind clicking.
    refusal_controls: bool = False
    #: Set once a red-team pass has tried and failed to satisfy this oracle
    #: without the true effect. A task is not release-eligible until True.
    adversarially_audited: bool = False


class TaskSpec(BaseModel):
    """One authored benchmark task (design doc sections 2-3).

    A task is substrate + category + a natural-language goal + an initial
    system-of-record state + the DECLARED expected effect + an
    :class:`OracleSpec`. It is deliberately agent-agnostic: every baseline arm
    receives the SAME ``goal`` (intent, never a step list) and is scored by the
    SAME oracle, so a difference between arms is the agent, not the harness.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    title: str = ""
    substrate: Substrate
    category: DivergenceCategory
    #: The natural-language intent handed identically to every learning agent
    #: (fairness requirement — never a step list).
    goal: str
    #: The DECLARED expected effect: what must be true of the system of record
    #: for this task to have actually succeeded. A runtime ``Effect`` contract,
    #: parameterized by run params via ``ValueExpr`` so the record actually
    #: written this trial is the record checked.
    expected_effect: Effect
    oracle: OracleSpec
    #: The system-of-record state to seed before each trial (channel-specific;
    #: e.g. rows to insert, a concurrent-actor row to plant for a C4 stale test).
    #: Empty means "start from the pinned container's clean state".
    initial_state: dict[str, Any] = Field(default_factory=dict)

    # -- structural variants that cut across all categories (section 2.1) ------
    #: Irreversible effects (a submitted claim) are where SWER is the money
    #: metric; reversible ones (a draft edit) are cheaper to recover.
    reversible: bool = True
    #: Whether this task ships a declared effect + configured verifier. The
    #: benchmark measures BOTH raw agents and the value of effect-verification,
    #: so both conditions must be sampled. ``False`` marks the raw-agent
    #: condition (the oracle still scores it; the agent just isn't given the
    #: verifier).
    effect_declared: bool = True

    #: Dev vs sequestered-test split (section 5.2). Test-split oracle configs /
    #: payloads are withheld from the public release.
    split: str = "dev"
    #: Free-form provenance / notes carried into the audit trail.
    notes: str = ""

    @property
    def is_control(self) -> bool:
        return self.category is DivergenceCategory.CONTROL


class ModelCall(BaseModel):
    """One model invocation inside an episode (for cost / latency accounting).

    The runner records one per API call so ``cost_usd`` is ``sum(tokens x pinned
    list price)`` and is auditable per-episode (design doc sections 5-6). A
    zero-model arm (the compiler) records an empty list.
    """

    model_config = ConfigDict(extra="forbid")

    index: int
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    note: str = ""


class AgentReport(BaseModel):
    """The agent arm's SELF-REPORT for one episode — never trusted for scoring.

    This is exactly the witness EffectBench refuses to believe: ``reported_success``
    is what the agent asserted or the screen rendered. The independent oracle,
    not this object, decides the outcome; the two are crossed only to detect the
    *silent* case (``reported_success`` while the oracle disagrees).
    """

    model_config = ConfigDict(extra="forbid")

    #: What the agent asserted / the screen rendered (a banner OCR, the agent's
    #: final "done", or a screen-only postcondition). The deceptive witness.
    reported_success: bool
    #: Whether the run stopped without completing (halt / refusal). Distinct
    #: from ``reported_success`` so a halt-with-failure and a report-of-failure
    #: are the same here but a report-of-success can never be a halt.
    halted: bool = False
    #: Optional final message / reason string for the audit trail.
    message: str = ""


class OracleVerdict(BaseModel):
    """The independent oracle's reading of the TRUE effect for one episode.

    A serialization-friendly projection of a runtime
    :class:`~openadapt_flow.runtime.effects.effect.EffectVerdict` plus the
    derived booleans :func:`classify_outcome` consumes. Persisted verbatim so a
    result row is self-describing and re-classifiable offline.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    kind: EffectKind
    channel: str = ""
    reason: str = ""
    observed_count: Optional[int] = None
    expected_count: Optional[int] = None
    #: Whether the pre-action baseline was readable (an unreadable baseline
    #: forces INDETERMINATE; a phantom vs wrong-write split needs it).
    before_reachable: bool = True
    #: The independent judgement: exactly one correct, complete effect landed.
    effect_correct: bool = False
    #: Something wrong persisted (duplicate / partial / lost update / wrong
    #: record) — distinct from nothing persisting.
    effect_wrong_persisted: bool = False
    #: Nothing (relevant) persisted — the phantom case.
    effect_absent: bool = False


class EpisodeRecord(BaseModel):
    """One trial of one task by one arm — the atomic benchmark result row.

    Every headline metric is a pure aggregation over a list of these (see
    :func:`openadapt_flow.benchmark.effectbench.metrics.summarize`). The runner
    produces one per (task x arm x trial); ``summarize`` never needs anything
    the row does not carry, so raw rows can be published for external replication.
    """

    model_config = ConfigDict(extra="forbid")

    episode_id: str
    task_id: str
    arm: str  # baseline name: "claude_cu" / "openai_cua" / "compiler" / ...
    trial: int
    substrate: Substrate
    category: DivergenceCategory

    #: Seed for this trial; the trial-unique payload is derived from it so the
    #: oracle checks THIS run's exact effect (section 5.2).
    seed: Optional[int] = None
    #: SHA-256 of the resolved expected-effect contract (audit; two trials that
    #: verified different records have different hashes).
    expected_effect_hash: str = ""

    #: Whether the correct action was in fact AVAILABLE this episode — the axis
    #: that separates OVER_HALT (available) from SAFE_HALT (not). Set by the
    #: task/environment, never inferred from the agent.
    correct_action_available: bool = True

    agent: AgentReport
    oracle: OracleVerdict
    outcome: OutcomeLabel
    swer_variant: SwerVariant = SwerVariant.NONE
    reason: str = ""

    #: Cost / latency accounting.
    model_calls: list[ModelCall] = Field(default_factory=list)
    cost_usd: float = 0.0
    latency_s: float = 0.0

    #: Environment fingerprint (OS / app version / DPI / theme) and pinned
    #: model/tool versions — recorded per episode for reproducibility (5.2).
    env_fingerprint: dict[str, Any] = Field(default_factory=dict)
    started_at: str = Field(default_factory=_utcnow)
    finished_at: str = Field(default_factory=_utcnow)

    # -- derived predicates the metrics layer aggregates -----------------------
    @property
    def is_silent_wrong(self) -> bool:
        """The SWER numerator: reported success while the record was not correct.

        Equivalent to ``outcome is SILENT_WRONG_EFFECT`` by construction of
        :func:`classify_outcome`; exposed as a predicate so a raw row loaded
        from JSON is self-checking without re-running the classifier.
        """
        return self.outcome is OutcomeLabel.SILENT_WRONG_EFFECT

    @property
    def is_over_halt(self) -> bool:
        return self.outcome is OutcomeLabel.OVER_HALT

    @property
    def is_effect_success(self) -> bool:
        """Effect-verified success — the honest task-success numerator."""
        return self.outcome is OutcomeLabel.SUCCESS

    @property
    def reported_success(self) -> bool:
        """The screen-only / self-reported success the gap measures against."""
        return self.agent.reported_success
