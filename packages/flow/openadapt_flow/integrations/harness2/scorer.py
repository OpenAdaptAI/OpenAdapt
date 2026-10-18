"""Score a GUI rollout by reading the system of record, through the reward worker.

The score is the reward worker's (``openadapt_flow.reward.worker``), the same
code ``openadapt-flow serve-reward`` runs: register the episode and read the
baseline before the agent starts, read the store again after it stops, judge
the declared effects, sign a receipt. Nothing here looks at the screen or at
what the agent said it did.

On top of the receipt this module adds what a harness search needs and the
receipt deliberately leaves out: which records were added, whose they are,
and a single number to rank candidates by. The number comes from
:class:`Penalties`, declared by the caller:

* ``verified``: every required effect landed, nothing else did.
* wrong record: a record landed on another subject.
* duplicate: the subject got more than one new record.
* wrong value: the subject's one new record holds values the task did not
  ask for.
* other ``wrong_effect``: the agent reported done, but no record landed.
* safe stop: ``halted_before_effect``, ``refused`` or ``rejected_policy`` with
  nothing written. Not a success, never a penalty.
* unscored (``reconciliation_required``, ``failed_platform``): no number. A
  person has to settle it, and a search must not read it as a zero.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.integrations.harness2.tasks import GuiTask
from openadapt_flow.reward.seed import write_bundle
from openadapt_flow.reward.worker import RewardWorker

#: Runtime signals an agent's final report can map to.
COMPLETED = "completed"
HALTED = "halted_before_effect"
FAILED_PLATFORM = "failed_platform"

_SAFE_STOPS = frozenset({"halted_before_effect", "refused", "rejected_policy"})
_UNSCORED = frozenset({"reconciliation_required", "failed_platform"})


@dataclass(frozen=True)
class Penalties:
    """The search scalar for each outcome. Declared, never guessed."""

    verified: float = 1.0
    safe_stop: float = 0.0
    wrong_effect: float = -0.5
    wrong_record: float = -1.0
    duplicate: float = -1.0


@dataclass
class EpisodeScore:
    """One rollout's read-back: the receipt plus the added-record breakdown."""

    episode_id: str
    outcome: str
    reason: str
    #: The signed receipt's own scalar (``None`` when unscored).
    receipt_reward: Optional[float]
    unscored: bool
    receipt_id: str
    oracle_tier: int
    #: Records this rollout added, as read from the store.
    added: list[dict[str, Any]] = field(default_factory=list)
    subject_added: int = 0
    other_added: int = 0
    wrong_record: bool = False
    duplicate: bool = False
    #: The subject got its one record, but with values the task did not ask for.
    wrong_value: bool = False
    safe_stop: bool = False
    #: The search scalar from :class:`Penalties`; ``None`` when unscored.
    reward: Optional[float] = None
    #: Whether the outcome is one the task counts as right.
    passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EpisodeScore":
        return cls(**data)


def reward_contract_effects(task: GuiTask) -> list[dict[str, Any]]:
    """The required effects for a single-record write task.

    One ``exact_new_set`` over the whole store: the rollout adds exactly one
    record, for the subject, with the task's values, and nothing else. A
    record on another subject, a second record, or a wrong value refutes it.
    """

    record: dict[str, Any] = {key: {"param": key} for key in sorted(task.identity)}
    for key in task.record_fields:
        if key in task.params:
            record[key] = task.params[key]
    return [
        {
            "kind": "exact_new_set",
            "match": {},
            "new_records": [record],
            "expected_count": 1,
        }
    ]


class ReadBackScorer:
    """A reward worker over one app copy, for one task."""

    def __init__(
        self,
        task: GuiTask,
        oracle_recipe: dict[str, Any],
        directory: Path,
        *,
        penalties: Penalties = Penalties(),
        subject_key: Optional[str] = None,
    ) -> None:
        self.task = task
        self.penalties = penalties
        # The field that says whose record it is: the identity's only key
        # unless the caller names one.
        if subject_key is None:
            if len(task.identity) != 1:
                raise ValueError("name subject_key when the identity has several keys")
            subject_key = next(iter(task.identity))
        self.subject_key = subject_key
        self.directory = Path(directory)
        bundle_dir = self.directory / "contract"
        # A synthetic, uncertified tier-2 contract for this copy of the app.
        # Its oracle names the copy's own URL, so its digest is per rollout.
        write_bundle(
            bundle_dir,
            contract_id="reward_contract_harness2_gui",
            oracle=oracle_recipe,
            channel="api" if oracle_recipe.get("kind") in ("rest", "fhir") else "db",
            key=Ed25519PrivateKey.generate(),
            issuer_key_id="self_signed:harness2-gui",
            certify=False,
            required_effects=reward_contract_effects(task),
            forbidden_effects=[],
            identity_keys=sorted(task.identity),
        )
        self.worker = RewardWorker(bundle_dir, self.directory / "worker")

    def begin(self, episode_id: str) -> None:
        """Register the subject and read the baseline. Call before the agent."""

        self.worker.begin_episode(episode_id, dict(self.task.identity))

    def score(self, episode_id: str, runtime_signal: str) -> EpisodeScore:
        """Read the store after the agent stopped, judge it, sign it."""

        envelope = self.worker.score_episode(
            {
                "episode_id": episode_id,
                "policy_checkpoint_id": "harness2_gui_policy",
                "policy_update": 0,
                "reward_contract_digest": self.worker.contract.digest,
                "metadata": {
                    "oracle_identity": dict(self.task.identity),
                    "runtime_signal": runtime_signal,
                },
            }
        )
        receipt = envelope["receipt"]
        evidence = self._evidence(receipt["receipt_id"])
        added = self._added(episode_id, evidence)
        subject = str(self.task.identity.get(self.subject_key, ""))
        subject_added = sum(
            1 for r in added if str(r.get(self.subject_key, "")) == subject
        )
        other_added = len(added) - subject_added
        wanted = {
            k: str(self.task.params[k])
            for k in self.task.record_fields
            if k in self.task.params
        }
        mine = [r for r in added if str(r.get(self.subject_key, "")) == subject]
        wrong_value = len(mine) == 1 and any(
            str(mine[0].get(k)) != v for k, v in wanted.items()
        )
        outcome = str(receipt["reward_outcome"])
        score = EpisodeScore(
            episode_id=episode_id,
            outcome=outcome,
            reason=str(evidence.get("reason", "")),
            receipt_reward=receipt.get("scalar_reward"),
            unscored=bool(envelope.get("unscored")),
            receipt_id=str(receipt["receipt_id"]),
            oracle_tier=int(receipt["oracle_tier"]),
            added=added,
            subject_added=subject_added,
            other_added=other_added,
            wrong_record=other_added > 0,
            duplicate=subject_added > 1,
            wrong_value=wrong_value,
            safe_stop=outcome in _SAFE_STOPS,
        )
        score.reward = self.search_reward(score)
        # A stray, duplicate or wrong write is never the right outcome, even
        # when the task accepts "a person reconciles it" (the timeout trap).
        score.passed = outcome in self.task.expected and not (
            score.wrong_record or score.duplicate or score.wrong_value
        )
        return score

    def search_reward(self, score: EpisodeScore) -> Optional[float]:
        """The ranking scalar; ``None`` for an outcome a person must settle."""

        p = self.penalties
        # A write on another subject, a second write on this one, or a record
        # with the wrong values is harmful whatever the agent reported. The
        # worker leaves "halted, but something landed" unscored for a person
        # to settle; when the read shows the landed record is a stray, a
        # duplicate or wrong, the search still ranks it with the penalty.
        if score.wrong_record or score.duplicate or score.wrong_value:
            worst = p.wrong_effect
            if score.wrong_record:
                worst = min(worst, p.wrong_record)
            if score.duplicate:
                worst = min(worst, p.duplicate)
            return worst
        if score.outcome in _UNSCORED or score.unscored:
            return None
        if score.outcome == "verified":
            return p.verified
        if score.safe_stop:
            return p.safe_stop
        return p.wrong_effect

    # -- evidence -------------------------------------------------------------

    def _evidence(self, receipt_id: str) -> dict[str, Any]:
        path = self.directory / "worker" / "rewards" / receipt_id / "evidence.json"
        return dict(json.loads(path.read_text(encoding="utf-8")))

    def _added(self, episode_id: str, evidence: dict[str, Any]) -> list[dict[str, Any]]:
        """Records in the post-read that the baseline did not hold, by ``id``."""

        baseline = self.directory / "worker" / "baselines" / f"{episode_id}.json"
        before = json.loads(baseline.read_text(encoding="utf-8"))
        before_ids = {
            str(r.get("id")) for r in before.get("state", {}).get("records", [])
        }
        observed = evidence.get("observed", {}).get("records", [])
        return [
            {k: v for k, v in r.items() if k in ("id", "patient_id", "type", "note")}
            for r in observed
            if isinstance(r, dict) and str(r.get("id")) not in before_ids
        ]


__all__ = [
    "COMPLETED",
    "EpisodeScore",
    "FAILED_PLATFORM",
    "HALTED",
    "Penalties",
    "ReadBackScorer",
    "reward_contract_effects",
]
