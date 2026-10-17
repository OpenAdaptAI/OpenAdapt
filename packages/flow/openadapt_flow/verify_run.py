"""Verify an effect that something other than Flow replay claims it made.

``openadapt-flow verify-run`` reads the system of record through an existing
:class:`~openadapt_flow.runtime.effects.EffectVerifier` and rules on a declared
effect contract. The executor can be a computer-use agent, a script, a person,
or a Flow replay. Its run record is bound into the receipt by digest and is
never read as evidence: the verdict comes from the read-back alone.

Three inputs, kept apart on purpose:

* ``verify.yaml``: the verifier wiring (the same keys as the ``effects:``
  section of ``deployment.yaml``) and the effect contract (the same list as
  ``effect_contract.yaml``).
* ``--params``: the values the authorized task named (which patient, which
  note). They come from whoever authorized the work, not from the executor,
  so the executor cannot choose what it is checked against.
* ``--run-record``: what the executor says it did. Hashed, never trusted.

The output is an ``openadapt.effect-receipt/v1`` document. Its ``receipt``
keeps every field of ``openadapt.execute-evidence-receipt/v1`` with the same
name, pattern, and meaning, adds ``executor_kind``, ``executor_ref_sha256``,
``effect_verdict``, and ``verification_tier``, and carries a new ``schema_version`` so a strict
Execute consumer refuses it instead of mistaking it for a governed Flow run.
``evidence_digest`` binds the evidence manifest written beside it.

The receipt is unsigned. An issuer adds ``seal_signature``
(``openadapt.seal-signature/v1``) over the same canonical hash Cloud uses for
Execute receipts, so the existing offline check verifies the signature
unchanged. Nothing here signs, uploads, or calls a model.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, Sequence, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.runtime.effects.adapter import verifier_identity
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
    Verdict,
)
from openadapt_flow.verification import (
    EffectContractMutationError,
    VerificationTier,
    oracle_tier_from_verification_tier,
    verifier_effect_tier,
    verify_effect_without_mutation,
)

EFFECT_RECEIPT_SCHEMA = "openadapt.effect-receipt/v1"
EFFECT_EVIDENCE_SCHEMA = "openadapt.effect-evidence/v1"
VERIFY_CONFIG_SCHEMA = "openadapt.verify/v1"
BASELINE_SCHEMA = "openadapt.effect-baseline/v1"

ExecutorKind = Literal["flow_replay", "agent", "script", "human", "unknown"]
EXECUTOR_KINDS: tuple[str, ...] = ("flow_replay", "agent", "script", "human", "unknown")

EffectVerdictLabel = Literal["CONFIRMED", "REFUTED", "INDETERMINATE"]

#: How the effect was checked. Only ``independent_readback`` can yield
#: ``verified``, and only when the read also went through the system of
#: record (the v1 oracle-tier rule).
ReadbackClass = Literal["independent_readback", "screen_readback", "unchecked"]

#: Verifier kinds that never read independently of the acting surface.
_NOT_INDEPENDENT = {"none", "onscreen"}

_STRENGTH_BY_TIER = {
    VerificationTier.INDEPENDENT_SYSTEM: "independent_system_of_record",
    VerificationTier.INDEPENDENT_SESSION: "independent_session",
    VerificationTier.PERSISTED_STATE_REACQUISITION: "persisted_state_reacquisition",
    VerificationTier.IMMEDIATE_SCREEN: "immediate_screen_confirmation",
}
_STRENGTH_RANK = {
    "immediate_screen_confirmation": 1,
    "persisted_state_reacquisition": 2,
    "independent_session": 3,
    "independent_system_of_record": 4,
}


class VerifyRunError(ValueError):
    """verify-run refused its inputs. Nothing was read or written."""


class VerifyConfig(BaseModel):
    """``verify.yaml``: what to read, and what must be true there."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.verify/v1"] = "openadapt.verify/v1"
    #: Opaque ids copied into the receipt. Defaults say plainly that nothing
    #: qualified this contract.
    qualification_id: str = Field(
        default="unqualified-verify-run", pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
    )
    environment_id: str = Field(
        default="unnamed-environment", pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
    )
    minimum_effect_strength: Literal[
        "independent_system_of_record", "independent_session"
    ] = "independent_system_of_record"
    #: Same shape as ``deployment.yaml`` ``effects:``.
    verifier: EffectsConfig
    #: Same shape as ``effect_contract.yaml`` ``effects:``.
    effects: list[Effect] = Field(min_length=1)


def load_verify_config(path: str | Path) -> VerifyConfig:
    import yaml

    source = Path(path)
    if not source.is_file():
        raise VerifyRunError(f"verify config {source} is not a file")
    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise VerifyRunError(f"verify config {source} must be a YAML mapping")
    try:
        cfg = VerifyConfig.model_validate(data)
    except Exception as exc:
        raise VerifyRunError(f"invalid verify config {source}: {exc}") from exc
    if cfg.verifier.kind in _NOT_INDEPENDENT:
        raise VerifyRunError(
            f"verifier kind {cfg.verifier.kind!r} does not read independently of "
            "the executor; verify-run needs a system-of-record or "
            "separate-session verifier"
        )
    if cfg.verifier.candidates:
        raise VerifyRunError("verify-run does not support verifier candidates yet")
    for effect in cfg.effects:
        if effect.needs_operator_confirmation:
            raise VerifyRunError(
                "the effect contract still holds an unconfirmed placeholder"
            )
        if effect.readback is not None:
            raise VerifyRunError(
                "an on-screen read-back effect cannot be verified after the fact"
            )
    return cfg


def _canonical(value: Any) -> bytes:
    """Sorted-key compact JSON; the Seal's canonical form."""

    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_tagged(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _write_private(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _build_verifier(cfg: VerifyConfig, params: Mapping[str, object]) -> Any:
    try:
        verifier = build_effect_verifier(cfg.verifier, params)
    except ValueError as exc:
        raise VerifyRunError(f"cannot build the verifier: {exc}") from exc
    if verifier is None:
        raise VerifyRunError("verify config built no verifier")
    return verifier


def capture_baseline(
    cfg: VerifyConfig, params: Mapping[str, object], out: Path
) -> EffectState:
    """Read the system of record before the executor starts.

    The baseline is what lets ``count_new_only`` and ``exact_new_set`` tell a
    record the executor added from one that was already there. It holds raw
    records, so it stays private on this machine: mode 0600, never inside a
    receipt. Only its digest is bound.
    """

    verifier = _build_verifier(cfg, params)
    state = verifier.capture_pre_state()
    if not isinstance(state, EffectState):
        raise VerifyRunError("verifier returned no readable baseline")
    _write_private(
        out,
        {
            "schema_version": BASELINE_SCHEMA,
            "verifier_identity": verifier_identity(verifier),
            "state": state.model_dump(mode="json"),
        },
    )
    return state


def _load_baseline(
    path: Optional[Path], verifier: Any
) -> tuple[EffectState, Optional[str]]:
    if path is None:
        # No baseline: the judge treats the pre-state as unknown, so every
        # claim about change is INDETERMINATE, never guessed.
        return EffectState(
            substrate=str(getattr(verifier, "substrate", "")), reachable=False
        ), None
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != BASELINE_SCHEMA:
        raise VerifyRunError(f"{path} is not an {BASELINE_SCHEMA} file")
    if raw.get("verifier_identity") != verifier_identity(verifier):
        raise VerifyRunError("baseline was captured by a different verifier")
    return EffectState.model_validate(raw["state"]), sha256_tagged(raw)


def _executor(run_record: Mapping[str, Any]) -> tuple[str, str]:
    kind = run_record.get("executor_kind")
    if kind not in EXECUTOR_KINDS:
        raise VerifyRunError(
            "run record must name executor_kind as one of " + ", ".join(EXECUTOR_KINDS)
        )
    executor_id = run_record.get("executor_id")
    ref = (
        executor_id if isinstance(executor_id, str) and executor_id else "unidentified"
    )
    return str(kind), "sha256:" + hashlib.sha256(ref.encode("utf-8")).hexdigest()


def _label(verdict: Verdict) -> EffectVerdictLabel:
    return {
        Verdict.CONFIRMED: "CONFIRMED",
        Verdict.REFUTED: "REFUTED",
        Verdict.INDETERMINATE: "INDETERMINATE",
    }[verdict]  # type: ignore[return-value]


def _aggregate(verdicts: Sequence[EffectVerdict]) -> EffectVerdictLabel:
    if any(v.verdict is Verdict.INDETERMINATE for v in verdicts):
        return "INDETERMINATE"
    if any(v.verdict is Verdict.REFUTED for v in verdicts):
        return "REFUTED"
    return "CONFIRMED"


def _observed(verdicts: Sequence[EffectVerdict]) -> str:
    seen = {v.observed_effect for v in verdicts}
    if "unknown" in seen:
        return "unknown"
    if "conflicting" in seen or seen == {"absent", "present"}:
        # A wrong record, or some effects landed and others did not.
        return "conflicting"
    return "absent" if "absent" in seen else "present"


def _readback_class(
    verdicts: Sequence[EffectVerdict], tiers: Sequence[Optional[VerificationTier]]
) -> ReadbackClass:
    """``unchecked`` unless every effect was actually read."""

    if any(t is None for t in tiers) or any(v.unavailable for v in verdicts):
        return "unchecked"
    if all(t is not None and t.is_independent_of_actuation() for t in tiers):
        return "independent_readback"
    return "screen_readback"


def verify_run(
    cfg: VerifyConfig,
    *,
    params: Mapping[str, object],
    run_record: Mapping[str, Any],
    authorization_ref: str,
    baseline_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Read the system of record and return ``{"receipt", "evidence"}``.

    The run record contributes ``executor_kind``, a digest of
    ``executor_id``, and a digest of itself. No other field of it is read.
    ``VERIFIED`` requires every effect CONFIRMED by a system-of-record read at
    or above ``minimum_effect_strength``; anything weaker is never upgraded.
    """

    from openadapt_types.execute import (
        EffectStrengthV1,
        ExecuteEvidenceContractV1,
        ExecuteEvidenceReceiptV1,
        ExecuteTerminalOutcomeV1,
    )

    if not isinstance(run_record, Mapping):
        raise VerifyRunError("run record must be a JSON object")
    executor_kind, executor_ref = _executor(run_record)
    run_record_digest = sha256_tagged(dict(run_record))
    verifier = _build_verifier(cfg, params)
    before, baseline_digest = _load_baseline(baseline_path, verifier)

    contract_digest = sha256_tagged(
        {
            "verifier_identity": verifier_identity(verifier),
            "effects": [e.model_dump(mode="json") for e in cfg.effects],
        }
    )
    verdicts: list[EffectVerdict] = []
    rows: list[dict[str, Any]] = []
    weakest: Optional[VerificationTier] = None
    tiers: list[Optional[VerificationTier]] = []
    for effect in cfg.effects:
        try:
            bound = effect.resolve(params)
        except ValueError as exc:
            raise VerifyRunError(f"effect contract cannot bind params: {exc}") from exc
        tier = verifier_effect_tier(verifier, bound)
        try:
            verdict = verify_effect_without_mutation(verifier, bound, before)
        except EffectContractMutationError as exc:
            raise VerifyRunError(str(exc)) from exc
        if tier is None and verdict.verdict is Verdict.CONFIRMED:
            # An untyped verifier cannot certify anything.
            verdict = EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=bound.kind,
                substrate=verdict.substrate,
                reason="verifier declares no evidence tier",
            )
        if tier is not None and (weakest is None or int(tier) > int(weakest)):
            weakest = tier
        verdicts.append(verdict)
        tiers.append(tier)
        # Closed fields only: no reason text, no observed value, no records.
        rows.append(
            {
                "effect_contract_hash": bound.contract_hash(),
                "kind": bound.kind.value,
                "substrate": verdict.substrate,
                "verification_tier": int(tier) if tier is not None else None,
                "verdict": _label(verdict.verdict),
                "observed_effect": verdict.observed_effect,
                "observed_count": verdict.observed_count,
                "expected_count": verdict.expected_count,
                "unavailable": verdict.unavailable,
                "stale": verdict.stale,
            }
        )

    effect_verdict = _aggregate(verdicts)
    observed_effect = _observed(verdicts)
    strength = _STRENGTH_BY_TIER.get(weakest) if weakest is not None else None
    strong_enough = strength is not None and (
        _STRENGTH_RANK[strength] >= _STRENGTH_RANK[cfg.minimum_effect_strength]
    )
    sor_read = weakest is VerificationTier.INDEPENDENT_SYSTEM
    verification_tier = _readback_class(verdicts, tiers)

    delivery_uncertain = False
    if (
        effect_verdict == "CONFIRMED"
        and verification_tier == "independent_readback"
        and strong_enough
        and sor_read
    ):
        outcome = ExecuteTerminalOutcomeV1.VERIFIED
    elif effect_verdict == "REFUTED" and observed_effect == "absent":
        # The store shows nothing landed, whatever the executor said.
        outcome = ExecuteTerminalOutcomeV1.HALTED_BEFORE_EFFECT
    else:
        # A wrong effect, an unreadable store, or a read too weak to certify.
        outcome = ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED
        delivery_uncertain = True
    verified = outcome is ExecuteTerminalOutcomeV1.VERIFIED

    evidence = {
        "schema_version": EFFECT_EVIDENCE_SCHEMA,
        "executor": {
            "kind": executor_kind,
            "ref_sha256": executor_ref,
            "run_record_sha256": run_record_digest,
            "claims_counted_as_evidence": False,
        },
        "authorization": {
            "reference_sha256": "sha256:"
            + hashlib.sha256(authorization_ref.encode("utf-8")).hexdigest(),
            "evaluated_by_verifier": False,
            # approver{kind, id, policy_ref}: placeholder until the
            # openadapt-types approver identity model exists (RFC-0002).
            "approver": None,
        },
        "contract": {
            "contract_sha256": contract_digest,
            "minimum_effect_strength": cfg.minimum_effect_strength,
        },
        "readback": {
            "verifier_identity": verifier_identity(verifier),
            "verification_tier": verification_tier,
            "baseline_sha256": baseline_digest,
            "read_at": _now(),
            "effects": rows,
        },
        "verifier": {
            "name": "openadapt-flow verify-run",
            "model_calls": 0,
        },
        "effect_verdict": effect_verdict,
    }
    contracts = ExecuteEvidenceContractV1(
        # The caller bound an authorization reference; verify-run did not
        # evaluate a policy. evidence.authorization says so.
        authorization_passed=verified,
        # Identity is the contract's record selector, judged in the store.
        identity_passed=verified,
        postcondition_passed=verified,
        effect_passed=verified,
        minimum_effect_strength=EffectStrengthV1(cfg.minimum_effect_strength),
        observed_effect_strength=(
            EffectStrengthV1(strength) if strength is not None else None
        ),
        # Nothing says the executor used no model, so the receipt cannot.
        model_used=executor_kind != "flow_replay",
        external_network_used=executor_kind != "flow_replay",
    )
    contract_hex = contract_digest.split(":", 1)[1]
    v1 = ExecuteEvidenceReceiptV1(
        receipt_id=f"receipt-{uuid4().hex}",
        execution_id=f"verify-{uuid4().hex}",
        workflow_digest=contract_digest,
        workflow_version=f"contract-{contract_hex[:16]}",
        qualification_id=cfg.qualification_id,
        environment_id=cfg.environment_id,
        runner_id=f"executor-{executor_ref.split(':', 1)[1][:16]}",
        nonce=f"nonce-{uuid4().hex}",
        oracle_tier=cast(
            Literal[0, 1, 2, 3],
            oracle_tier_from_verification_tier(weakest) if weakest is not None else 0,
        ),
        outcome=outcome,
        contracts=contracts,
        delivery_uncertain=delivery_uncertain,
        evidence_digest=sha256_tagged(evidence),
        issued_at=_now(),
    )
    receipt = v1.model_dump(mode="json", exclude={"seal_signature"})
    receipt["schema_version"] = EFFECT_RECEIPT_SCHEMA
    receipt["executor_kind"] = executor_kind
    receipt["executor_ref_sha256"] = executor_ref
    receipt["effect_verdict"] = effect_verdict
    receipt["verification_tier"] = verification_tier
    return {
        "schema_version": EFFECT_RECEIPT_SCHEMA,
        "receipt": receipt,
        "evidence": evidence,
    }


def write_result(result: Mapping[str, Any], out_dir: Path) -> Path:
    out = Path(out_dir) / "effect-receipt.json"
    _write_private(out, result)
    return out


def check_effect_receipt(
    document: Mapping[str, Any], registry: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Offline check: pinned-key Seal signature, then the evidence binding.

    The signature check is ``openadapt_types.seal.verify_sealed_receipt``,
    the same code path ``openadapt seal verify`` mirrors. A missing signature
    is reported as ``unsigned`` and is never a pass.
    """

    from openadapt_types.seal import parse_key_registry, verify_sealed_receipt

    receipt = document.get("receipt") if "receipt" in document else document
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema_version") != EFFECT_RECEIPT_SCHEMA
    ):
        return {"verified": False, "reason": "no_effect_receipt"}
    if receipt.get("outcome") in {"verified", "rolled_back_verified"} and (
        receipt.get("verification_tier") != "independent_readback"
        or receipt.get("effect_verdict") != "CONFIRMED"
    ):
        return {"verified": False, "reason": "malformed"}
    result: dict[str, Any] = {
        "verified": False,
        "executor_kind": receipt.get("executor_kind"),
        "effect_verdict": receipt.get("effect_verdict"),
        "outcome": receipt.get("outcome"),
    }
    evidence = document.get("evidence")
    if isinstance(evidence, Mapping):
        if sha256_tagged(dict(evidence)) != receipt.get("evidence_digest"):
            return {**result, "reason": "evidence_mismatch"}
        if evidence.get("effect_verdict") != receipt.get("effect_verdict"):
            return {**result, "reason": "evidence_mismatch"}
        if (evidence.get("executor") or {}).get("kind") != receipt.get("executor_kind"):
            return {**result, "reason": "evidence_mismatch"}
        if (evidence.get("readback") or {}).get("verification_tier") != receipt.get(
            "verification_tier"
        ):
            return {**result, "reason": "evidence_mismatch"}
        result["evidence_bound"] = True
    else:
        result["evidence_bound"] = False
    seal = verify_sealed_receipt(receipt, parse_key_registry(list(registry)))
    if not seal.ok:
        return {**result, "reason": seal.reason}
    signature = receipt.get("seal_signature") or {}
    if signature.get("run_id") != receipt.get("execution_id"):
        return {**result, "reason": "receipt_mismatch"}
    return {**result, "verified": True, "reason": "verified", "key_id": seal.key_id}
