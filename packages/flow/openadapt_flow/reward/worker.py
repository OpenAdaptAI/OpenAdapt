"""The reward worker: read the store, judge, score, sign.

One worker holds one reward contract bundle, one oracle adapter, and one
local signing key. ``score_episode`` is the whole path:

1. settle which subject is graded and check its keys against the contract,
   then check that the episode's policy update has not gone backwards;
2. read the system of record through the oracle (one read, after the
   episode ended; an optional baseline read before it started);
3. judge every required effect and every forbidden effect with the shared
   three-valued judge (``runtime/effects/_common.py``);
4. map the verdicts and the runtime's own signal onto ``RewardOutcomeV1``;
5. call the pure ``openadapt_types.reward.score`` helper;
6. write the evidence locally, sign the receipt, store both.

INDETERMINATE never becomes 0. It becomes an unscored outcome with a
stated uncertainty, and the trainer drops the sample.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional, cast
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openadapt_types.oracle import OracleAdapter, OracleObservation
from openadapt_types.process_capability import canonical_json_bytes
from openadapt_types.reward import (
    REWARD_SCORING_CLASS,
    RewardCertificateV1,
    RewardContractV1,
    RewardEvidenceReceiptV1,
    RewardOutcomeV1,
    RewardScoringClassV1,
    RewardUncertaintyStateV1,
    certificate_state,
    score,
)
from pydantic import ValidationError

from openadapt_flow.execute.keys import (
    fingerprint_of,
    load_or_create_private_key,
    load_or_create_token,
)
from openadapt_flow.reward.models import (
    EpisodeDescriptorV1,
    IdentityError,
    RewardBundle,
    SelfSignedRewardEnvelopeV1,
    assert_no_forbidden_keys,
)
from openadapt_flow.reward.oracles import (
    build_oracle,
    effect_state_of,
    records_of,
)
from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    Verdict,
)


class RewardWorkerError(Exception):
    """Typed failure with an HTTP status for the reference server."""

    def __init__(self, status_code: int, error: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.error = error
        self.detail = detail

    def body(self) -> dict[str, str]:
        return {"error": self.error, "detail": self.detail}


class Judgement:
    """The outcome mapping's result before scoring."""

    __slots__ = ("outcome", "uncertainty", "required", "forbidden", "reason")

    def __init__(
        self,
        outcome: RewardOutcomeV1,
        uncertainty: RewardUncertaintyStateV1,
        required: list[EffectVerdict],
        forbidden: list[EffectVerdict],
        reason: str,
    ) -> None:
        self.outcome = outcome
        self.uncertainty = uncertainty
        self.required = required
        self.forbidden = forbidden
        self.reason = reason


_SIGNAL_OUTCOME: dict[str, RewardOutcomeV1] = {
    "halted_before_effect": RewardOutcomeV1.HALTED_BEFORE_EFFECT,
    "refused": RewardOutcomeV1.REFUSED,
    "rejected_policy": RewardOutcomeV1.REJECTED_POLICY,
}


def judge_episode(
    bundle: RewardBundle,
    identity: dict[str, str],
    runtime_signal: str,
    before: EffectState,
    observed: OracleObservation,
) -> Judgement:
    """Map oracle verdicts and the runtime's signal onto a reward outcome.

    The table, in the order the rules fire:

    * runtime signal ``failed_platform`` -> ``FAILED_PLATFORM`` (unscored).
    * store unreachable -> ``FAILED_PLATFORM``, uncertainty
      ``oracle_unavailable`` (unscored).
    * any required or forbidden verdict INDETERMINATE for another reason ->
      ``RECONCILIATION_REQUIRED``, uncertainty ``effect_uncertain``
      (unscored).
    * any forbidden effect present -> ``WRONG_EFFECT``.
    * signal ``completed``: every required effect CONFIRMED -> ``VERIFIED``;
      any REFUTED -> ``WRONG_EFFECT`` (the banner lie lands here: the screen
      said saved, the store holds nothing).
    * signal ``halted_before_effect`` / ``refused`` / ``rejected_policy``:
      no required effect present -> that outcome; a required effect present
      anyway -> ``RECONCILIATION_REQUIRED`` with ``effect_uncertain``, since
      the runtime and the store disagree and a person settles it.
    """

    substrate = bundle.oracle.channel.value
    if runtime_signal == "failed_platform":
        return Judgement(
            RewardOutcomeV1.FAILED_PLATFORM,
            RewardUncertaintyStateV1.NONE,
            [],
            [],
            "runtime reported a platform failure",
        )
    current = records_of(observed)
    if current is None:
        return Judgement(
            RewardOutcomeV1.FAILED_PLATFORM,
            RewardUncertaintyStateV1.ORACLE_UNAVAILABLE,
            [],
            [],
            "system of record unreachable at read time",
        )
    required = [
        judge_records(_bind(effect, identity), before, current, substrate=substrate)
        for effect in bundle.required_effects
    ]
    forbidden = [
        judge_records(_bind(effect, identity), before, current, substrate=substrate)
        for effect in bundle.forbidden_effects
    ]
    indeterminate = [
        verdict
        for verdict in (*required, *forbidden)
        if verdict.verdict is Verdict.INDETERMINATE
    ]
    if indeterminate:
        return Judgement(
            RewardOutcomeV1.RECONCILIATION_REQUIRED,
            RewardUncertaintyStateV1.EFFECT_UNCERTAIN,
            required,
            forbidden,
            indeterminate[0].reason,
        )
    present_forbidden = [v for v in forbidden if _forbidden_present(v)]
    if present_forbidden:
        return Judgement(
            RewardOutcomeV1.WRONG_EFFECT,
            RewardUncertaintyStateV1.NONE,
            required,
            forbidden,
            "a forbidden effect is present in the system of record",
        )
    all_confirmed = all(v.verdict is Verdict.CONFIRMED for v in required)
    any_present = any(_required_present(v) for v in required)
    if runtime_signal == "completed":
        if all_confirmed:
            return Judgement(
                RewardOutcomeV1.VERIFIED,
                RewardUncertaintyStateV1.NONE,
                required,
                forbidden,
                "every required effect is present exactly as declared",
            )
        refuted = next(v for v in required if v.verdict is Verdict.REFUTED)
        return Judgement(
            RewardOutcomeV1.WRONG_EFFECT,
            RewardUncertaintyStateV1.NONE,
            required,
            forbidden,
            refuted.reason,
        )
    signalled = _SIGNAL_OUTCOME[runtime_signal]
    if any_present:
        return Judgement(
            RewardOutcomeV1.RECONCILIATION_REQUIRED,
            RewardUncertaintyStateV1.EFFECT_UNCERTAIN,
            required,
            forbidden,
            f"runtime reported {runtime_signal} but a required effect is present",
        )
    return Judgement(
        signalled,
        RewardUncertaintyStateV1.NONE,
        required,
        forbidden,
        f"runtime reported {runtime_signal}; the store shows no required effect",
    )


def _bind(effect: Effect, identity: dict[str, str]) -> Effect:
    return effect.resolve(identity)


def _required_present(verdict: EffectVerdict) -> bool:
    if verdict.verdict is Verdict.CONFIRMED:
        return True
    return bool(verdict.observed_count) or bool(verdict.matched_records)


def _forbidden_present(verdict: EffectVerdict) -> bool:
    """A forbidden effect is present when the store holds any matching record."""

    if verdict.kind is EffectKind.FIELD_EQUALS:
        return verdict.verdict is Verdict.CONFIRMED
    if verdict.verdict is Verdict.CONFIRMED:
        return True
    return bool(verdict.observed_count) or bool(verdict.matched_records)


class RewardWorker:
    """One reward contract, one oracle, one local key, on one machine."""

    def __init__(
        self,
        bundle: RewardBundle | Path | str,
        data_dir: Path | str,
        *,
        oracle: OracleAdapter | None = None,
        token: str | None = None,
    ) -> None:
        self.bundle = (
            bundle if isinstance(bundle, RewardBundle) else RewardBundle.load(bundle)
        )
        self.data_dir = Path(data_dir).expanduser()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._key: Ed25519PrivateKey = load_or_create_private_key(self.data_dir)
        self.token = load_or_create_token(self.data_dir, token)
        self.fingerprint = fingerprint_of(self._key.public_key())
        self.oracle: OracleAdapter = oracle or build_oracle(
            self.bundle.oracle, self.bundle.directory
        )
        if self.oracle.channel is not self.bundle.oracle.channel:
            raise ValueError(
                "oracle adapter channel does not match the contract's channel"
            )
        (self.data_dir / "rewards").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "baselines").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "policy_updates").mkdir(parents=True, exist_ok=True)

    # -- public surface -----------------------------------------------------

    @property
    def contract(self) -> RewardContractV1:
        return self.bundle.contract

    @property
    def certificate(self) -> Optional[RewardCertificateV1]:
        return self.bundle.certificate

    @property
    def issuer_key_id(self) -> str:
        return "self_signed:" + self.fingerprint

    def begin_episode(self, episode_id: str, identity: dict[str, str]) -> EffectState:
        """Register the episode's oracle identity and capture the baseline.

        Call it before the rollout runs. Two things happen here and both
        matter at scoring time.

        The baseline is what lets a ``count_new_only`` effect tell a record
        this episode wrote from one that was already there; without it that
        effect judges INDETERMINATE and the episode is unscored.

        The identity is the subject the episode is about, fixed before the
        rollout produced anything. :meth:`score_episode` uses this one, not
        the one the descriptor carries, so the graded subject cannot be
        chosen after the outcome is known.

        Calling it again for the same episode with the same identity is
        allowed and re-reads the baseline. Calling it again with a different
        identity is refused: the subject is not a thing an episode changes
        its mind about part way through.
        """

        self.bundle.check_identity(identity)
        with self._lock:
            scored = self._episode_index(episode_id)
            if scored is not None:
                raise RewardWorkerError(
                    409,
                    "duplicate_episode",
                    f"episode already scored as receipt {scored}; a scored "
                    "episode cannot be re-registered",
                )
            registered, _ = self._baseline(episode_id)
            if registered is not None and registered != dict(identity):
                raise RewardWorkerError(
                    409,
                    "identity_conflict",
                    f"episode {episode_id} is already registered for "
                    f"{_identity_text(registered)}; re-registering it for "
                    f"{_identity_text(dict(identity))} would move the subject "
                    "after the fact",
                )
            observed = self.oracle.read(identity)
            state = effect_state_of(observed, self.bundle.oracle.channel.value)
            self._write_json(
                self.data_dir / "baselines" / f"{episode_id}.json",
                {"identity": dict(identity), "state": state.model_dump(mode="json")},
            )
        return state

    def score_episode(
        self, payload: dict[str, Any] | EpisodeDescriptorV1
    ) -> dict[str, Any]:
        """Read, judge, score, sign. Returns the stored envelope as a dict."""

        try:
            episode = (
                payload
                if isinstance(payload, EpisodeDescriptorV1)
                else EpisodeDescriptorV1.model_validate(payload)
            )
            declared_identity = episode.resolved_identity()
            signal = episode.resolved_signal()
        except (ValidationError, ValueError) as exc:
            raise RewardWorkerError(422, "invalid_episode", str(exc)) from exc
        self._check_binding(episode)
        with self._lock:
            existing = self._episode_index(episode.episode_id)
            if existing is not None:
                raise RewardWorkerError(
                    409,
                    "duplicate_episode",
                    f"episode already scored as receipt {existing}",
                )
            registered_identity, before = self._baseline(episode.episode_id)
            identity = self._graded_identity(declared_identity, registered_identity)
            try:
                self.bundle.check_identity(identity)
            except IdentityError as exc:
                raise RewardWorkerError(422, "identity_mismatch", str(exc)) from exc
            self._check_policy_update(episode)
            observed = self.oracle.read(identity)
            judged = judge_episode(self.bundle, identity, signal, before, observed)
            envelope = self._issue(episode, observed, before, judged)
            self._advance_policy_update(episode)
        return envelope

    def _graded_identity(
        self,
        declared: Optional[dict[str, str]],
        registered: Optional[dict[str, str]],
    ) -> dict[str, str]:
        """Pick the subject this episode is graded on. Registration wins.

        The registration was made by the environment before the rollout ran,
        when nobody knew how the episode would turn out. The descriptor
        arrives afterwards from the trainer, which by then does know. So the
        registration decides, and a descriptor that names a different subject
        is refused rather than quietly overridden: a trainer that believes it
        is grading one subject while the worker grades another has a bug
        worth stopping for.
        """

        if registered is None:
            if declared is None:
                raise RewardWorkerError(
                    422,
                    "identity_missing",
                    "the episode names no oracle identity: pass oracle_identity, "
                    "metadata.oracle_identity, or register it with begin_episode",
                )
            return declared
        if declared is not None and declared != registered:
            raise RewardWorkerError(
                422,
                "identity_conflict",
                f"the environment registered this episode for "
                f"{_identity_text(registered)} before the rollout ran, and "
                f"the descriptor names {_identity_text(declared)}. The "
                "registration decides which subject is graded; a descriptor "
                "may repeat it but may not replace it.",
            )
        return registered

    def _check_binding(self, episode: EpisodeDescriptorV1) -> None:
        if episode.reward_contract_digest != self.contract.digest:
            raise RewardWorkerError(
                422,
                "contract_mismatch",
                f"this worker serves contract {self.contract.digest}, the episode "
                f"names {episode.reward_contract_digest}",
            )
        if episode.task_id is not None and episode.task_id != self.contract.task_id:
            raise RewardWorkerError(
                422, "contract_mismatch", "task_id does not match the contract"
            )
        if (
            episode.environment_id is not None
            and episode.environment_id != self.contract.environment_id
        ):
            raise RewardWorkerError(
                422, "contract_mismatch", "environment_id does not match the contract"
            )

    def get_receipt(self, receipt_id: str) -> dict[str, Any]:
        path = self.data_dir / "rewards" / receipt_id / "envelope.json"
        if not path.is_file():
            raise RewardWorkerError(404, "not_found", "no such reward receipt")
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert_no_forbidden_keys(payload.get("receipt") or {})
        return cast(
            dict[str, Any],
            SelfSignedRewardEnvelopeV1.model_validate(payload).model_dump(mode="json"),
        )

    def verify_receipt(self, receipt: RewardEvidenceReceiptV1) -> bool:
        """Check a receipt's signature against this worker's key."""

        from cryptography.exceptions import InvalidSignature

        try:
            self._key.public_key().verify(
                base64.b64decode(receipt.signature),
                canonical_json_bytes(receipt.unsigned_payload()),
            )
        except (InvalidSignature, ValueError):
            return False
        return True

    # -- issue --------------------------------------------------------------

    def _issue(
        self,
        episode: EpisodeDescriptorV1,
        observed: OracleObservation,
        before: EffectState,
        judged: Judgement,
    ) -> dict[str, Any]:
        tier = observed.tier
        scored = score(
            judged.outcome,
            tier,
            self.certificate,
            episode.policy_update,
            scoring=self.contract.scoring,
        )
        state = certificate_state(self.certificate, episode.policy_update)
        receipt_id = _new_id("reward_receipt")
        evidence = {
            "episode_id": episode.episode_id,
            "oracle_channel": observed.channel.value,
            "oracle_identity": dict(observed.identity),
            "baseline_reachable": before.reachable,
            "observed": observed.value,
            "required_verdicts": [v.model_dump(mode="json") for v in judged.required],
            "forbidden_verdicts": [v.model_dump(mode="json") for v in judged.forbidden],
            "reason": judged.reason,
        }
        evidence_digest = (
            "sha256:" + hashlib.sha256(canonical_json_bytes(evidence)).hexdigest()
        )
        components: dict[str, float] = {}
        if scored.scalar is not None:
            total = sum(c.weight for c in self.contract.components)
            for component in self.contract.components:
                components[component.name] = scored.scalar * component.weight / total
        unsigned = {
            "schema_version": "openadapt.reward-evidence-receipt/v1",
            "receipt_id": receipt_id,
            "reward_contract_digest": self.contract.digest,
            "policy_checkpoint_id": episode.policy_checkpoint_id,
            "policy_update": episode.policy_update,
            "episode_id": episode.episode_id,
            "oracle_tier": int(tier),
            "reward_outcome": judged.outcome.value,
            "evidence_digest": evidence_digest,
            "reward_components": components,
            "scalar_reward": scored.scalar,
            "certificate_id": (
                self.certificate.certificate_id
                if self.certificate is not None
                else None
            ),
            "certificate_digest": (
                self.certificate.digest if self.certificate is not None else None
            ),
            "certificate_state": state.value,
            "calibration_corpus_digest": (
                self.certificate.calibration_corpus_digest
                if self.certificate is not None
                else None
            ),
            "calibration_scope": (
                self.certificate.calibration_scope.value
                if self.certificate is not None
                else None
            ),
            "uncertainty": judged.uncertainty.value,
            "certified": scored.certified,
            "development_only": scored.development_only,
            "issuer_key_id": self.issuer_key_id,
            "nonce": _new_id("nonce"),
            "issued_at": _now(),
        }
        signature = base64.b64encode(
            self._key.sign(canonical_json_bytes(unsigned))
        ).decode("ascii")
        receipt = RewardEvidenceReceiptV1.model_validate(
            {**unsigned, "signature_algorithm": "ed25519", "signature": signature}
        )
        payload = receipt.model_dump(mode="json")
        assert_no_forbidden_keys(payload)
        envelope = SelfSignedRewardEnvelopeV1(
            issuer_key_fingerprint=self.fingerprint,
            unscored=REWARD_SCORING_CLASS[judged.outcome]
            is RewardScoringClassV1.UNSCORED,
            receipt=payload,
        )
        directory = self.data_dir / "rewards" / receipt_id
        self._write_json(directory / "evidence.json", evidence)
        self._write_json(directory / "receipt.json", payload)
        envelope_payload = envelope.model_dump(mode="json")
        self._write_json(directory / "envelope.json", envelope_payload)
        self._write_json(
            self.data_dir / "episodes" / f"{episode.episode_id}.json",
            {"receipt_id": receipt_id},
        )
        return envelope_payload

    # -- storage ------------------------------------------------------------

    def _baseline(
        self, episode_id: str
    ) -> tuple[Optional[dict[str, str]], EffectState]:
        path = self.data_dir / "baselines" / f"{episode_id}.json"
        if path.is_file():
            payload = json.loads(path.read_text("utf-8"))
            identity = {str(k): str(v) for k, v in dict(payload["identity"]).items()}
            return identity, EffectState.model_validate(payload["state"])
        # No baseline was captured: the delta is unknowable, so any effect
        # that needs one (count_new_only, exact_new_set) judges INDETERMINATE.
        return None, EffectState(
            substrate=self.bundle.oracle.channel.value, reachable=False
        )

    def _episode_index(self, episode_id: str) -> Optional[str]:
        path = self.data_dir / "episodes" / f"{episode_id}.json"
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return str(payload.get("receipt_id") or "") or None

    # -- the policy-update ledger -------------------------------------------

    def _ledger_path(self) -> Path:
        """One file per contract, beside the episode index."""

        stem = hashlib.sha256(self.contract.digest.encode("utf-8")).hexdigest()[:32]
        return self.data_dir / "policy_updates" / f"{stem}.json"

    def _ledger(self) -> dict[str, Any]:
        path = self._ledger_path()
        if not path.is_file():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}

    def _check_policy_update(self, episode: EpisodeDescriptorV1) -> None:
        """Refuse a descriptor whose policy update goes backwards.

        A certificate expires after a stated number of policy updates, and
        ``policy_update`` is the only thing that moves the episode towards
        that expiry. It arrives on the wire from the trainer, so without a
        record of its own the worker would let a trainer count 0, 999,
        1000000000, 0 and read the certificate as current again at the end.
        The ledger keeps the highest update this contract has seen and
        refuses anything below it, so the counter only moves the way that
        expires a certificate.

        The mark is per contract, not per policy checkpoint. Keeping it per
        checkpoint would let a trainer reset the count by starting to call
        its checkpoint something else, and expiry would never arrive. Policy
        updates count one training run against one contract, so a new
        checkpoint at a lower update is going back in time either way. The
        ledger records which checkpoint set the mark, for the error message.
        """

        ledger = self._ledger()
        mark = ledger.get("highest_policy_update")
        if not isinstance(mark, int) or episode.policy_update >= mark:
            return
        owner = str(ledger.get("policy_checkpoint_id") or "an earlier checkpoint")
        raise RewardWorkerError(
            422,
            "policy_update_regressed",
            f"this contract has already scored policy update {mark} (from "
            f"{owner}) and the episode names {episode.policy_update}. A "
            "policy update counter only moves forward; moving it back would "
            "make an expired certificate read as current.",
        )

    def _advance_policy_update(self, episode: EpisodeDescriptorV1) -> None:
        ledger = self._ledger()
        mark = ledger.get("highest_policy_update")
        if isinstance(mark, int) and mark >= episode.policy_update:
            return
        self._write_json(
            self._ledger_path(),
            {
                "reward_contract_digest": self.contract.digest,
                "highest_policy_update": episode.policy_update,
                "policy_checkpoint_id": episode.policy_checkpoint_id,
            },
        )

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)


def _identity_text(identity: dict[str, str]) -> str:
    return "{" + ", ".join(f"{k}={v}" for k, v in sorted(identity.items())) + "}"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def default_data_dir() -> Path:
    from openadapt_flow.reward import DEFAULT_DATA_DIRNAME

    return Path.home() / ".openadapt" / DEFAULT_DATA_DIRNAME


OutcomeLiteral = Literal[
    "verified",
    "halted_before_effect",
    "refused",
    "rejected_policy",
    "wrong_effect",
    "reconciliation_required",
    "failed_platform",
]
