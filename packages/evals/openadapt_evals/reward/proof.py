"""The MockMed reward proof: no model, no GPU, two rewards, one lie each.

Scripted policies produce rollouts against the in-memory MockMed store from
``openadapt_evals.extradup``: the gold CREATE (``control``) and each ExtraDup
family (``dup``, ``extra``, ``omit``, ``unsubmit``, ``claim``), plus
``identity_swap`` (a correct-looking note written onto the wrong
``patient_id``) and ``oracle_outage`` (the write is correct but the system of
record cannot be read). Every condition runs at least three trials on a fixed
seed schedule. The seed picks the banner wording and the receipt identities;
the store logic is deterministic. No model is trained and no GPU is used.

Two rewards score every rollout through the same ``assess_receipt`` path the
TRL and verl adapters use:

* ``visual_only``: oracle tier 0. It believes the banner, the filled form, or
  the policy's own claim. It is ``development_only`` and never certified.
* ``certified_sor``: oracle tier 2. It reads the named ``oracle.identity_keys``
  and runs ExtraDup's ``sor_check`` (record count, no extra field, every spec
  field) on that record, behind a self-signed certificate of synthetic scope.
  When the oracle is unavailable it returns ``failed_platform``, which is
  unscored. On ``identity_swap`` the named record is unchanged, so the scalar
  is 0 even though a correct-looking row landed on a decoy identity.

The certificate's epsilon is not invented. A calibration pass counts the
certified reward's false accepts over the gold-FAIL rollouts of this run and
the certificate carries the exact one-sided 95% Clopper-Pearson upper bound
on that count. It bounds this synthetic run and nothing else.

The 2026-09-01 committed files are the ExtraDup-only snapshot (no
``identity_swap``). Pass ``--conditions`` with that frozen set to regenerate
them. The default run includes ``identity_swap``.

Run::

    python -m openadapt_evals.reward.proof --json out.json --markdown out.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import random
from base64 import b64encode
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from openadapt_types.reward import (
    RewardCertificateV1,
    RewardContractV1,
    RewardEvidenceReceiptV1,
    RewardOutcomeV1,
    RewardScoringPolicyV1,
)

from openadapt_evals.extradup.checkers import new_records, sor_check
from openadapt_evals.extradup.gold import MOCKMED_GOLD
from openadapt_evals.extradup.mutations import OPERATORS, apply
from openadapt_evals.extradup.store import MockMedStore, Screen
from openadapt_evals.reward.devsigner import DevelopmentSigner, sha256_digest
from openadapt_evals.reward.receipts import ScoredEpisode, assess_receipt

logger = logging.getLogger(__name__)

PROOF_NAME = "reward-proof-mockmed-extradup"
PROOF_SCHEMA = 1
DEFAULT_SEEDS: tuple[int, ...] = (101, 202, 303)
MIN_TRIALS = 3
ORACLE_OUTAGE = "oracle_outage"
IDENTITY_SWAP = "identity_swap"
# Named identity the certified oracle reads. Matches RewardContractV1.oracle.
ORACLE_IDENTITY_KEYS: tuple[str, ...] = ("patient_id", "type")
# Decoy the identity-swap policy writes onto. Not the gold patient_id.
WRONG_PATIENT_ID = "p2"
# ExtraDup families plus oracle_outage. The 2026-09-01 committed proof.
PROOF_2026_09_01_CONDITIONS: tuple[str, ...] = tuple(OPERATORS) + (ORACLE_OUTAGE,)
# Default run: the 09-01 set plus identity_swap, still before the outage.
CONDITIONS: tuple[str, ...] = tuple(OPERATORS) + (IDENTITY_SWAP, ORACLE_OUTAGE)
VISUAL = "visual_only"
CERTIFIED = "certified_sor"
REWARDS: tuple[str, ...] = (VISUAL, CERTIFIED)
REWARD_TIER = {VISUAL: 0, CERTIFIED: 2}
POLICY_UPDATE = 7
POLICY_CHECKPOINT_ID = "policy.checkpoint.proof-scripted"
EXPIRY_POLICY_UPDATES = 100
BANNER_VARIANTS: tuple[str, ...] = (
    "Saved",
    "Encounter saved",
    "Record saved successfully",
    "Saved to chart",
)
ISSUED_AT = "2026-09-01T00:00:00Z"
# The proof's scoring policy. Wrong effect pays 0, not the default -1, so the
# table reads in {0, 1}: "paid" means scalar > 0.
SCORING = RewardScoringPolicyV1(wrong_effect_reward=0.0)


@dataclass(frozen=True)
class Rollout:
    condition: str
    trial: int
    seed: int
    episode_id: str
    operator: str
    gold: str
    before: list[dict[str, Any]]
    after: list[dict[str, Any]]
    screen: dict[str, bool]
    banner_text: str
    oracle_available: bool

    @property
    def evidence_digest(self) -> str:
        return sha256_digest(
            {
                "before": self.before,
                "after": self.after,
                "screen": self.screen,
                "banner_text": self.banner_text,
            }
        )

    @property
    def new_count(self) -> int:
        return len(new_records(self.before, self.after))


@dataclass
class ProofRun:
    seeds: tuple[int, ...]
    trials: int
    conditions: tuple[str, ...]
    contract: RewardContractV1
    certificate: RewardCertificateV1
    public_key_b64: str
    rollouts: list[Rollout]
    receipts: dict[tuple[str, int, str], RewardEvidenceReceiptV1]
    scored: dict[tuple[str, int, str], ScoredEpisode]
    calibration: dict[str, Any]
    expiry_check: dict[str, Any]
    table: list[dict[str, Any]] = field(default_factory=list)


# -- scripted policies ------------------------------------------------------------------


def gold_for(condition: str) -> str:
    """The gold verdict of the write. ``oracle_outage`` writes correctly."""

    return "PASS" if condition in {"control", ORACLE_OUTAGE} else "FAIL"


def named_oracle_identity() -> dict[str, str]:
    """The record the certified oracle reads, keyed as the contract's identity_keys."""

    return {key: str(MOCKMED_GOLD.fields[key]) for key in ORACLE_IDENTITY_KEYS}


def records_for_identity(
    rows: Sequence[dict[str, Any]], identity: dict[str, str]
) -> list[dict[str, Any]]:
    """Rows whose identity_keys match ``identity``. The named record, nothing else."""

    return [
        dict(row)
        for row in rows
        if all(str(row.get(key, "")) == value for key, value in identity.items())
    ]


def _write_wrong_identity(
    store: MockMedStore,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Screen]:
    """Gold note text, gold type, decoy patient_id. Banner still says saved."""

    store.reset()
    before = store.snapshot()
    fields = dict(MOCKMED_GOLD.fields)
    fields["patient_id"] = WRONG_PATIENT_ID
    screen = store.write(fields)
    return before, store.snapshot(), screen


def scripted_rollout(condition: str, trial: int, seed: int) -> Rollout:
    if condition not in CONDITIONS:
        raise KeyError(f"unknown proof condition {condition!r}")
    rng = random.Random(f"{seed}:{condition}:{trial}")
    store = MockMedStore()
    if condition == IDENTITY_SWAP:
        operator = IDENTITY_SWAP
        before, after, screen = _write_wrong_identity(store)
    else:
        operator = "control" if condition == ORACLE_OUTAGE else condition
        before, after, screen = apply(store, MOCKMED_GOLD, operator)
    banner_text = rng.choice(BANNER_VARIANTS) if screen.banner_saved else ""
    return Rollout(
        condition=condition,
        trial=trial,
        seed=seed,
        episode_id=f"proof.{condition}.t{trial}.s{seed}",
        operator=operator,
        gold=gold_for(condition),
        before=before,
        after=after,
        screen={
            "banner_saved": screen.banner_saved,
            "form_looks_complete": screen.form_looks_complete,
            "claimed_success": screen.claimed_success,
        },
        banner_text=banner_text,
        oracle_available=condition != ORACLE_OUTAGE,
    )


def scripted_rollouts(
    seeds: Sequence[int],
    trials: int,
    conditions: Sequence[str] = CONDITIONS,
) -> list[Rollout]:
    if trials < MIN_TRIALS:
        raise ValueError(f"at least {MIN_TRIALS} trials per condition")
    if len(seeds) < trials:
        raise ValueError("the seed schedule must have one seed per trial")
    selected = tuple(conditions)
    unknown = [item for item in selected if item not in CONDITIONS]
    if unknown:
        raise KeyError(f"unknown proof condition {unknown[0]!r}")
    if not selected:
        raise ValueError("need at least one condition")
    return [
        scripted_rollout(condition, trial, seeds[trial])
        for condition in selected
        for trial in range(trials)
    ]


# -- the two rewards ----------------------------------------------------------------------


def visual_outcome(rollout: Rollout) -> RewardOutcomeV1:
    """Tier 0. Pays when the screen says the write happened."""

    banner_says_saved = "saved" in rollout.banner_text.lower()
    looks_done = (
        banner_says_saved
        or rollout.screen["form_looks_complete"]
        or rollout.screen["claimed_success"]
    )
    return RewardOutcomeV1.VERIFIED if looks_done else RewardOutcomeV1.WRONG_EFFECT


def certified_outcome(rollout: Rollout) -> RewardOutcomeV1:
    """Tier 2. Reads the named identity; unavailable oracle is unscored.

    The worker needs ``oracle.identity_keys`` to know which record to read.
    ``identity_swap`` writes a correct-looking note onto a decoy patient_id;
    the named record is unchanged, so this returns ``wrong_effect``.
    """

    if not rollout.oracle_available:
        return RewardOutcomeV1.FAILED_PLATFORM
    identity = named_oracle_identity()
    named_before = records_for_identity(rollout.before, identity)
    named_after = records_for_identity(rollout.after, identity)
    if sor_check(MOCKMED_GOLD, named_before, named_after).ok:
        return RewardOutcomeV1.VERIFIED
    return RewardOutcomeV1.WRONG_EFFECT


OUTCOME_FN = {VISUAL: visual_outcome, CERTIFIED: certified_outcome}


# -- calibration ---------------------------------------------------------------------------


def clopper_pearson_upper(false_accepts: int, trials: int, confidence: float = 0.95) -> float:
    """Exact one-sided upper bound on a binomial proportion.

    The smallest ``p`` with ``P(X <= k | n, p) <= 1 - confidence``. With
    ``k == n`` the bound is 1.0; with ``k == 0`` it is ``1 - (1-c)^(1/n)``.
    """

    if not 0 <= false_accepts <= trials or trials <= 0:
        raise ValueError("need 0 <= false_accepts <= trials and trials > 0")
    if false_accepts == trials:
        return 1.0
    alpha = 1.0 - confidence

    def cdf(p: float) -> float:
        return sum(
            math.comb(trials, i) * p**i * (1.0 - p) ** (trials - i)
            for i in range(false_accepts + 1)
        )

    low, high = 0.0, 1.0
    for _ in range(200):
        mid = (low + high) / 2.0
        if cdf(mid) > alpha:
            low = mid
        else:
            high = mid
    return round(high, 6)


def calibrate(rollouts: Sequence[Rollout]) -> dict[str, Any]:
    """Count each reward's false accepts over the gold-FAIL rollouts."""

    gold_fail = [item for item in rollouts if item.gold == "FAIL"]
    result: dict[str, Any] = {
        "gold_fail_trials": len(gold_fail),
        "false_accepts": {},
        "clopper_pearson_upper_95": {},
    }
    for reward in REWARDS:
        paid = sum(1 for item in gold_fail if OUTCOME_FN[reward](item) is RewardOutcomeV1.VERIFIED)
        result["false_accepts"][reward] = paid
        result["clopper_pearson_upper_95"][reward] = clopper_pearson_upper(paid, len(gold_fail))
    result["calibration_corpus_digest"] = sha256_digest(
        {
            "episodes": [item.episode_id for item in gold_fail],
            "evidence": [item.evidence_digest for item in gold_fail],
        }
    )
    return result


def build_contract(calibration: dict[str, Any]) -> RewardContractV1:
    epsilon = calibration["clopper_pearson_upper_95"][CERTIFIED]
    # A certificate cannot bound at epsilon >= 1.0 (the contract forbids it);
    # a reward that paid every mutant would have no certificate at all.
    if not 0.0 < epsilon < 1.0:
        raise ValueError(f"the certified reward's bound {epsilon} admits no certificate")
    spec = {
        "collection": MOCKMED_GOLD.collection,
        "fields": dict(MOCKMED_GOLD.fields),
        "expected_new": MOCKMED_GOLD.expected_new,
        "allowed_fields": sorted(MOCKMED_GOLD.allowed_fields),
    }
    return RewardContractV1.model_validate(
        {
            "contract_id": "reward.contract.proof-mockmed-extradup",
            "contract_version": "reward.contract.proof-mockmed-extradup.v1",
            "task_id": "task.mockmed.triage-save",
            "task_digest": sha256_digest(spec),
            "environment_id": "environment.mockmed.in-memory",
            "environment_digest": sha256_digest(b"openadapt_evals.extradup.store.MockMedStore"),
            "required_effect_contract_digest": sha256_digest(
                {"new_records": 1, "fields": spec["fields"]}
            ),
            "forbidden_effect_contract_digest": sha256_digest(
                {"extra_record": True, "extra_field": True, "missing_field": True}
            ),
            "oracle": {
                "channel": "db",
                "identity_keys": list(ORACLE_IDENTITY_KEYS),
                "oracle_contract_digest": sha256_digest(
                    b"openadapt_evals.extradup.checkers.sor_check"
                ),
            },
            "components": [{"name": "terminal_effect", "weight": 1.0}],
            "scoring": SCORING.model_dump(mode="json"),
            "certificate_policy": {
                "epsilon": epsilon,
                "delta": 0.05,
                # sor_check is an exact match on count and fields; there is
                # no tunable threshold. 1.0 records "exact".
                "threshold": 1.0,
                "calibration_corpus_digest": calibration["calibration_corpus_digest"],
                "expiry_policy_updates": EXPIRY_POLICY_UPDATES,
            },
        }
    )


# -- receipts -------------------------------------------------------------------------------


def issue_receipts(
    signer: DevelopmentSigner,
    contract: RewardContractV1,
    certificate: RewardCertificateV1,
    rollouts: Sequence[Rollout],
    *,
    policy_update: int = POLICY_UPDATE,
) -> dict[tuple[str, int, str], RewardEvidenceReceiptV1]:
    receipts: dict[tuple[str, int, str], RewardEvidenceReceiptV1] = {}
    for rollout in rollouts:
        for reward in REWARDS:
            tier = REWARD_TIER[reward]
            key = (rollout.condition, rollout.trial, reward)
            nonce = hashlib.sha256(f"{rollout.episode_id}:{reward}".encode()).hexdigest()[:24]
            receipts[key] = signer.issue_receipt(
                contract=contract,
                receipt_id=f"receipt.{rollout.episode_id}.{reward}",
                episode_id=rollout.episode_id,
                policy_checkpoint_id=POLICY_CHECKPOINT_ID,
                policy_update=policy_update,
                oracle_tier=tier,
                outcome=OUTCOME_FN[reward](rollout),
                evidence_digest=rollout.evidence_digest,
                nonce=f"nonce.{nonce}",
                issued_at=ISSUED_AT,
                certificate=certificate if tier >= 2 else None,
                scoring=SCORING,
            )
    return receipts


class _WarningCounter(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.expired = 0

    def emit(self, record: logging.LogRecord) -> None:
        if "expired" in record.getMessage():
            self.expired += 1


def expiry_check(
    contract: RewardContractV1,
    certificate: RewardCertificateV1,
    receipts: dict[tuple[str, int, str], RewardEvidenceReceiptV1],
) -> dict[str, Any]:
    """Re-assess the certified control receipts once the certificate has expired."""

    update = certificate.expires_at_policy_update
    counter = _WarningCounter()
    receipts_logger = logging.getLogger("openadapt_evals.reward.receipts")
    receipts_logger.addHandler(counter)
    try:
        still_certified = 0
        checked = 0
        for (condition, _trial, reward), receipt in sorted(receipts.items()):
            if reward != CERTIFIED or condition != "control":
                continue
            episode = assess_receipt(
                receipt,
                policy_update=update,
                expected_contract_digest=contract.digest,
                certificate=certificate,
                certificate_policy=contract.certificate_policy,
            )
            checked += 1
            still_certified += int(episode.certified)
    finally:
        receipts_logger.removeHandler(counter)
    return {
        "policy_update": update,
        "receipts_checked": checked,
        "certified_after_expiry": still_certified,
        "expiry_warnings_logged": counter.expired,
    }


# -- the run ---------------------------------------------------------------------------------


def run_proof(
    seeds: Sequence[int] = DEFAULT_SEEDS,
    trials: int = MIN_TRIALS,
    conditions: Sequence[str] | None = None,
) -> ProofRun:
    seeds = tuple(int(seed) for seed in seeds)
    selected = tuple(conditions) if conditions is not None else CONDITIONS
    rollouts = scripted_rollouts(seeds, trials, selected)
    calibration = calibrate(rollouts)
    contract = build_contract(calibration)
    signer = DevelopmentSigner(b"seeds:" + ",".join(str(seed) for seed in seeds).encode())
    certificate = signer.issue_certificate(
        contract,
        certificate_id="reward.certificate.proof-synthetic",
        checker_configuration_digest=sha256_digest(
            {"checker": "openadapt_evals.extradup.checkers.sor_check", "spec": "MOCKMED_GOLD"}
        ),
        issued_at_policy_update=0,
        issued_at=ISSUED_AT,
    )
    receipts = issue_receipts(signer, contract, certificate, rollouts)
    scored: dict[tuple[str, int, str], ScoredEpisode] = {}
    for key, receipt in receipts.items():
        scored[key] = assess_receipt(
            receipt,
            policy_update=POLICY_UPDATE,
            expected_contract_digest=contract.digest,
            expected_episode_id=receipt.episode_id,
            certificate=certificate if key[2] == CERTIFIED else None,
            certificate_policy=contract.certificate_policy,
        )
    run = ProofRun(
        seeds=seeds,
        trials=trials,
        conditions=selected,
        contract=contract,
        certificate=certificate,
        public_key_b64=b64encode(signer.public_key_bytes()).decode("ascii"),
        rollouts=rollouts,
        receipts=receipts,
        scored=scored,
        calibration=calibration,
        expiry_check=expiry_check(contract, certificate, receipts),
    )
    run.table = tabulate(run)
    return run


def tabulate(run: ProofRun) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for condition in run.conditions:
        gold = gold_for(condition)
        for reward in REWARDS:
            episodes = [run.scored[(condition, trial, reward)] for trial in range(run.trials)]
            paid = sum(1 for item in episodes if item.scalar is not None and item.scalar > 0)
            unscored = sum(1 for item in episodes if item.unscored)
            refused = sum(1 for item in episodes if item.scalar is not None and item.scalar <= 0)
            scopes = {item.calibration_scope for item in episodes}
            rows.append(
                {
                    "condition": condition,
                    "gold": gold,
                    "reward": reward,
                    "oracle_tier": REWARD_TIER[reward],
                    "trials": len(episodes),
                    "paid": paid,
                    "silent_incorrect_success_rate": (
                        round(paid / len(episodes), 4) if gold == "FAIL" else None
                    ),
                    "over_refusal_rate": (
                        round(refused / len(episodes), 4) if gold == "PASS" else None
                    ),
                    "unscored": unscored,
                    "certified": all(item.certified for item in episodes),
                    "calibration_scope": (
                        next(iter(scopes)) if len(scopes) == 1 and None not in scopes else None
                    ),
                }
            )
    return rows


def _named_sor_reasons(item: Rollout) -> list[str]:
    identity = named_oracle_identity()
    named_before = records_for_identity(item.before, identity)
    named_after = records_for_identity(item.after, identity)
    return list(sor_check(MOCKMED_GOLD, named_before, named_after).reasons)


def _rollout_payload(run: ProofRun, item: Rollout) -> dict[str, Any]:
    payload: dict[str, Any] = {
        **{k: v for k, v in asdict(item).items() if k not in {"before", "after"}},
        "new_count": item.new_count,
        "spec_count": MOCKMED_GOLD.expected_new,
        "sor_reasons": _named_sor_reasons(item),
        "evidence_digest": item.evidence_digest,
        "rewards": {
            reward: {
                "receipt_id": run.receipts[(item.condition, item.trial, reward)].receipt_id,
                **run.scored[(item.condition, item.trial, reward)].metadata(),
                "scalar": run.scored[(item.condition, item.trial, reward)].scalar,
            }
            for reward in REWARDS
        },
    }
    if item.condition == IDENTITY_SWAP:
        identity = named_oracle_identity()
        named_before = records_for_identity(item.before, identity)
        named_after = records_for_identity(item.after, identity)
        payload["oracle_identity"] = identity
        payload["written_patient_id"] = WRONG_PATIENT_ID
        payload["named_new_count"] = len(new_records(named_before, named_after))
    return payload


# -- output ------------------------------------------------------------------------------------


def _package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def to_json(run: ProofRun) -> dict[str, Any]:
    certificate = run.certificate
    return {
        "proof": PROOF_NAME,
        "schema": PROOF_SCHEMA,
        "seed_schedule": list(run.seeds),
        "trials_per_condition": run.trials,
        "conditions": list(run.conditions),
        "policy_update": POLICY_UPDATE,
        "scoring": SCORING.model_dump(mode="json"),
        "versions": {
            "openadapt-evals": _package_version("openadapt-evals"),
            "openadapt-types": _package_version("openadapt-types"),
        },
        "contract": {
            "digest": run.contract.digest,
            "certificate_policy": run.contract.certificate_policy.model_dump(mode="json"),
        },
        "certificate": {
            "certificate_id": certificate.certificate_id,
            "digest": certificate.digest,
            "epsilon": certificate.epsilon,
            "delta": certificate.delta,
            "threshold": certificate.threshold,
            "calibration_scope": certificate.calibration_scope.value,
            "issued_at_policy_update": certificate.issued_at_policy_update,
            "expiry_policy_updates": certificate.expiry_policy_updates,
            "issuer_key_id": certificate.issuer_key_id,
            "issuer": certificate.issuer.value,
            "issuer_public_key_ed25519_b64": run.public_key_b64,
        },
        "calibration": run.calibration,
        "table": run.table,
        "expiry_check": run.expiry_check,
        "rollouts": [_rollout_payload(run, item) for item in run.rollouts],
    }


def _fmt_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def to_markdown_table(rows: Sequence[dict[str, Any]]) -> str:
    header = (
        "| condition | gold | reward | tier | trials | paid | silent incorrect success | "
        "over-refusal | unscored | certified | scope |"
    )
    sep = "|---|---|---|---|---|---|---|---|---|---|---|"
    lines = [header, sep]
    for row in rows:
        lines.append(
            "| {condition} | {gold} | {reward} | {tier} | {trials} | {paid} | {sis} | {over} | "
            "{unscored} | {certified} | {scope} |".format(
                condition=row["condition"],
                gold=row["gold"],
                reward=row["reward"],
                tier=row["oracle_tier"],
                trials=row["trials"],
                paid=row["paid"],
                sis=_fmt_rate(row["silent_incorrect_success_rate"]),
                over=_fmt_rate(row["over_refusal_rate"]),
                unscored=row["unscored"],
                certified="yes" if row["certified"] else "no",
                scope=row["calibration_scope"] or "none",
            )
        )
    return "\n".join(lines)


def to_markdown(run: ProofRun, *, date: str) -> str:
    payload = to_json(run)
    cal = run.calibration
    fa = cal["false_accepts"]
    cp = cal["clopper_pearson_upper_95"]
    n = cal["gold_fail_trials"]
    versions = payload["versions"]
    lines = [
        f"# Reward proof on MockMed ExtraDup, {date}",
        "",
        f"Generated by `python -m openadapt_evals.reward.proof` with seed schedule "
        f"{list(run.seeds)} and {run.trials} trials per condition, scored at policy "
        f"update {POLICY_UPDATE}. openadapt-evals {versions['openadapt-evals']}, "
        f"openadapt-types {versions['openadapt-types']}. No model was trained and no "
        "GPU was used. The store is the in-memory MockMed encounter table from "
        "`openadapt_evals.extradup`; every record is synthetic.",
        "",
        f"Contract digest `{run.contract.digest}`. Certificate "
        f"`{run.certificate.certificate_id}`, scope **{payload['certificate']['calibration_scope']}**, "
        f"self-signed by the harness key `{run.certificate.issuer_key_id}` "
        f"(ed25519 public key `{run.public_key_b64}`), epsilon {run.certificate.epsilon}, "
        f"delta {run.certificate.delta}, current for {run.certificate.expiry_policy_updates} "
        "policy updates from update 0.",
        "",
        "## Table",
        "",
        "`paid` counts trials with scalar > 0. `silent incorrect success` is paid over trials "
        "on a gold-FAIL condition. `over-refusal` is scalar <= 0 over trials on a gold-PASS "
        "condition. `unscored` trials carry no scalar and are dropped by the adapters, never "
        "scored 0. `certified` is the adapter's recomputed flag, not the receipt's.",
        "",
        to_markdown_table(run.table),
        "",
        "## Calibration",
        "",
        f"Over the {n} gold-FAIL rollouts ({_fail_family_phrase(run)}, {run.trials} trials each):",
        "",
        f"- `{VISUAL}` paid {fa[VISUAL]} of {n}. One-sided 95% Clopper-Pearson upper bound "
        f"on its false-accept rate: {cp[VISUAL]}.",
        f"- `{CERTIFIED}` paid {fa[CERTIFIED]} of {n}. One-sided 95% Clopper-Pearson upper "
        f"bound: {cp[CERTIFIED]}. This number is the certificate's epsilon.",
        "",
        "The bound covers this synthetic run only. It is not a production epsilon and it "
        "says nothing about any real system of record.",
        "",
        "## Expiry",
        "",
        f"The {run.expiry_check['receipts_checked']} certified control receipts were re-assessed "
        f"at policy update {run.expiry_check['policy_update']}, the first update at which the "
        f"certificate is expired. {run.expiry_check['certified_after_expiry']} stayed certified; "
        f"the adapter logged {run.expiry_check['expiry_warnings_logged']} expiry warnings.",
        "",
        "## Reading it",
        "",
        _reading_paragraph(run),
        "",
        "The proof JSON beside this file holds every rollout, receipt id, and flag. "
        "Scripted policies, no GPU, not a trained policy, not a Production Seal.",
        "",
    ]
    return "\n".join(lines)


def _fail_family_phrase(run: ProofRun) -> str:
    fail = [item for item in run.conditions if gold_for(item) == "FAIL"]
    extradup = [item for item in fail if item in OPERATORS]
    extra = [item for item in fail if item not in OPERATORS]
    parts: list[str] = []
    if extradup:
        noun = "family" if len(extradup) == 1 else "families"
        count = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}.get(
            len(extradup), str(len(extradup))
        )
        parts.append(f"{count} ExtraDup {noun}")
    if extra:
        parts.append(" plus ".join(extra))
    return " plus ".join(parts) if parts else "no gold-FAIL families"


def _reading_paragraph(run: ProofRun) -> str:
    if IDENTITY_SWAP in run.conditions:
        mutants = (
            "The visual reward pays every mutant: the banner says saved after a duplicate "
            "CREATE, an extra field, a dropped field, or a correct-looking note written onto "
            f"the wrong patient_id ({WRONG_PATIENT_ID}), the filled form looks complete when "
            "nothing was posted, and the claim condition is the policy's own report."
        )
    else:
        mutants = (
            "The visual reward pays every mutant: the banner says saved after a duplicate "
            "CREATE, an extra field, or a dropped field, the filled form looks complete when "
            "nothing was posted, and the claim condition is the policy's own report."
        )
    outage = (
        " It also pays the outage condition, where the write was correct but nobody could check."
    )
    certified = (
        " The certified reward pays the control and refuses the ExtraDup families on the record "
        "count and field set"
    )
    if IDENTITY_SWAP in run.conditions:
        identity = named_oracle_identity()
        certified += (
            f"; on identity_swap it reads the named identity_keys {list(ORACLE_IDENTITY_KEYS)} "
            f"({identity}) and the named record is unchanged, so the scalar is 0"
        )
    certified += (
        "; on the outage it returns `failed_platform`, so those trials are "
        "unscored and leave the training group instead of teaching the policy that an "
        "unreadable store is worth 0."
    )
    return mutants + outage + certified


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m openadapt_evals.reward.proof",
        description="Score scripted MockMed rollouts with a visual-only and a certified reward.",
    )
    parser.add_argument("--json", type=Path, help="write the full run as JSON")
    parser.add_argument("--markdown", type=Path, help="write the human table as Markdown")
    parser.add_argument("--date", default="2026-09-02", help="date stamped in the Markdown title")
    parser.add_argument("--trials", type=int, default=MIN_TRIALS)
    parser.add_argument(
        "--seeds",
        default=",".join(str(seed) for seed in DEFAULT_SEEDS),
        help="comma-separated seed schedule, one seed per trial",
    )
    parser.add_argument(
        "--conditions",
        default="",
        help=(
            "comma-separated conditions (default: the full set, including identity_swap). "
            "Pass the 2026-09-01 set to regenerate that frozen proof: "
            + ",".join(PROOF_2026_09_01_CONDITIONS)
        ),
    )
    args = parser.parse_args(argv)
    seeds = tuple(int(item) for item in args.seeds.split(",") if item.strip())
    selected = tuple(item.strip() for item in args.conditions.split(",") if item.strip())
    run = run_proof(seeds, args.trials, selected or None)
    if args.json:
        args.json.write_text(json.dumps(to_json(run), indent=2, sort_keys=True) + "\n")
    if args.markdown:
        args.markdown.write_text(to_markdown(run, date=args.date))
    print(to_markdown_table(run.table))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
