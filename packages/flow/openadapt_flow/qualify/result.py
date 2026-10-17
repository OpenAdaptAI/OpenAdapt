"""``openadapt.qualification-result/v1`` and how ``--certify`` binds it.

RFC-0002 ("What certify signs"). The result is the ``QualificationReport``
without ``workflow_name`` (free text), with refusal codes only, plus what the
campaign itself observed:

- ``verify_config_sha256``: the read-back contract the cases were judged
  against, SHA-256 over ``{"verifier_identity", "effects"}`` (the save's
  effects as bound for the campaign), the same form as an effect receipt's
  ``workflow_digest``;
- ``verifier_identity``: ``openadapt.effect-verifier-identity/v1`` digest;
- ``fault_driver``: which driver staged the faults;
- ``cases``: one entry per trial, each verdict taken from the independent
  read-back, never from the run report;
- ``silent_incorrect_success_count`` (must be 0), ``over_halt_count`` and
  ``model_calls``.

Signing adds no scheme. ``certify`` binds the result's digest into the
existing ``openadapt.qualification-admission/v1`` envelope
(``campaign.outcomes_sha256``, ``campaign.oracle_contract_sha256``,
``campaign.oracle_id``) and signs that envelope with the well-known local-dev
qualification key, which every production trust map refuses. A production
admission is signed by the production authority from the same result.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Optional
from uuid import NAMESPACE_URL, uuid5

from openadapt_flow.verification import VerificationTier

RESULT_SCHEMA = "openadapt.qualification-result/v1"
FAULT_DRIVER_KIND = "web-generic"

Verdict = Literal["CONFIRMED", "REFUTED", "INDETERMINATE"]

#: What each case must observe in the system of record.
EXPECTED_VERDICT: Mapping[str, Verdict] = {
    "representative": "CONFIRMED",
    "fault-ambiguity": "REFUTED",
    "fault-wrong-identity": "REFUTED",
    "fault-stale-identity": "REFUTED",
    "fault-weak-effect": "REFUTED",
    "fault-missing-effect": "REFUTED",
}
FAULT_CLASS: Mapping[str, Optional[str]] = {
    "representative": None,
    "fault-ambiguity": "ambiguity",
    "fault-wrong-identity": "wrong_identity",
    "fault-stale-identity": "stale_identity",
    "fault-weak-effect": "weak_effect",
    "fault-missing-effect": "missing_effect",
}


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


# -- the read-back contract ----------------------------------------------------
def verifier_identity_of(cfg: Any, verifier: Any) -> str:
    """``openadapt.effect-verifier-identity/v1`` of the configured read-back."""

    from openadapt_flow.deployment import _effect_verifier_config_identity
    from openadapt_flow.runtime.effects.adapter import verifier_identity

    try:
        existing = verifier_identity(verifier)
    except Exception:  # noqa: BLE001 - not set on a hook-built client
        existing = None
    return existing or _effect_verifier_config_identity(cfg, verifier)


def verify_config_sha256(verifier_identity: str, effects: list[Any]) -> str:
    """``sha256:`` over ``{"verifier_identity", "effects": [Effect JSON]}``."""

    payload = {
        "verifier_identity": verifier_identity,
        "effects": [effect.model_dump(mode="json") for effect in effects],
    }
    return f"sha256:{sha256_hex(payload)}"


def readback_tier(tier: Optional[VerificationTier], independent: bool) -> str:
    """RFC-0002 ``verification_tier`` for one trial's read."""

    if tier is None or not independent:
        return "unchecked"
    if tier.is_independent_of_actuation():
        return "independent_readback"
    return "screen_readback"


# -- one trial -----------------------------------------------------------------
def observed_verdict(oracle: Mapping[str, Any]) -> Verdict:
    """The verdict the independent read gave, from the trial's oracle."""

    if oracle.get("independent") is False:
        return "INDETERMINATE"
    verdict = oracle.get("effect_verdict")
    if verdict in ("CONFIRMED", "REFUTED", "INDETERMINATE"):
        return verdict  # type: ignore[return-value]
    observed = oracle.get("observed")
    if observed == "exact_record":
        return "CONFIRMED"
    if observed in ("no_mutation", "record_mismatch", "unexpected_mutation"):
        return "REFUTED"
    return "INDETERMINATE"


def _outcome(verdict: Verdict, tier: str, landed: str, strongest: bool) -> str:
    if verdict == "CONFIRMED":
        return (
            "verified"
            if tier == "independent_readback" and strongest
            else "reconciliation_required"
        )
    if verdict == "REFUTED" and landed == "none":
        return "halted_before_effect"
    return "reconciliation_required"


def case_entry(
    *,
    case_id: str,
    trial: int,
    report: Any,
    oracle: Mapping[str, Any],
    verifier_tier: Optional[VerificationTier],
    report_path: Path,
) -> dict[str, Any]:
    """One ``cases[]`` entry. The verdict never reads the run report."""

    expected = EXPECTED_VERDICT[case_id]
    observed = observed_verdict(oracle)
    tier = readback_tier(verifier_tier, oracle.get("independent", True) is not False)
    landed = oracle.get("landed") or (
        "none" if oracle.get("observed") == "no_mutation" else "some"
    )
    entry = {
        "case_id": case_id,
        "trial": trial,
        "fault_class": FAULT_CLASS[case_id],
        "executor_kind": "flow_replay",
        "expected_verdict": expected,
        "observed_verdict": observed,
        "verification_tier": tier,
        "outcome": _outcome(
            observed,
            tier,
            landed,
            verifier_tier is VerificationTier.INDEPENDENT_SYSTEM,
        ),
        # The run report is the record this trial produced today. An
        # openadapt.effect-receipt/v1 replaces it once openadapt-types ships it.
        "receipt_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "evidence_digest": f"sha256:{sha256_hex(dict(oracle))}",
        "engine_outcome": report.execution_outcome,
        "model_calls": int(report.model_calls or 0),
    }
    entry["passed"] = bool(observed == expected and tier == "independent_readback")
    entry["silent_incorrect_success"] = bool(
        (expected != "CONFIRMED" and observed == "CONFIRMED")
        or (report.success and observed != expected)
    )
    entry["over_halt"] = bool(
        expected == "CONFIRMED" and report.execution_outcome == "HALTED"
    )
    return entry


# -- the result ----------------------------------------------------------------
def build_result(
    *,
    report: Any,
    verify_sha256: str,
    verifier_identity: str,
    fault_driver_version: str,
    cases: list[dict[str, Any]],
    judge_model_calls: int = 0,
) -> dict[str, Any]:
    """``openadapt.qualification-result/v1`` from the project's report."""

    base = dict(report.model_dump(mode="json"))
    base.pop("workflow_name", None)  # free text stays in the local report
    base.pop("schema_version", None)
    base["refusals"] = sorted(
        {
            json.dumps(
                {
                    k: v
                    for k, v in {
                        "code": r["code"],
                        "path": r.get("path"),
                        "step_id": r.get("step_id"),
                        "case_id": r.get("case_id"),
                    }.items()
                    if v is not None
                },
                sort_keys=True,
            )
            for r in base.get("refusals", [])
        }
    )
    base["refusals"] = [json.loads(item) for item in base["refusals"]]
    silent = sum(1 for c in cases if c["silent_incorrect_success"])
    over_halt = sum(1 for c in cases if c["over_halt"])
    passed = bool(
        base["passed"] and silent == 0 and cases and all(c["passed"] for c in cases)
    )
    return {
        "schema_version": RESULT_SCHEMA,
        **base,
        "passed": passed,
        "verify_config_sha256": verify_sha256,
        "verifier_identity": verifier_identity,
        "fault_driver": {"kind": FAULT_DRIVER_KIND, "version": fault_driver_version},
        "cases": cases,
        "silent_incorrect_success_count": silent,
        "over_halt_count": over_halt,
        "model_calls": judge_model_calls
        + sum(int(c.get("model_calls", 0)) for c in cases),
    }


def result_sha256(result: Mapping[str, Any]) -> str:
    """Digest of the result without ``generated_at`` (unprefixed hex)."""

    return sha256_hex({k: v for k, v in result.items() if k != "generated_at"})


# -- binding through the admission envelope -------------------------------------
def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hex(value: Optional[str], label: str) -> str:
    """An unprefixed digest, or a labelled placeholder for an unbound field."""

    if value:
        return value.split(":", 1)[-1]
    return hashlib.sha256(f"openadapt.qualify.unbound.{label}".encode()).hexdigest()


def local_admission(
    *,
    result: Mapping[str, Any],
    result_bytes: bytes,
    oracle_id: str,
    campaign_id: str,
    trials_required: int,
    bundle_content_digest: str,
    effect_contract_sha256: str,
    identity_contract_sha256: Optional[str],
    now: Optional[datetime] = None,
) -> Any:
    """Bind ``result`` into a ``qualification-admission/v1`` envelope.

    Signed with the local-dev qualification key. Fields that only a managed
    deployment knows (tenant, workflow version, runtime validation,
    authorization template, input/action/network/operator policy) are
    deterministic placeholders. Raises ``ValueError`` when the campaign does
    not meet the admission's own floor (three trials per case).
    """

    from openadapt_flow.qualification_admission import QualificationAdmissionPayload
    from openadapt_flow.qualification_dev_signer import (
        LOCAL_DEV_ISSUER_WORKFLOW,
        LOCAL_DEV_KEY_ID,
        LOCAL_DEV_REF_PREFIX,
        sign_production_shaped_local_admission,
    )

    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    observed: dict[str, int] = {}
    for case in result["cases"]:
        observed[case["case_id"]] = observed.get(case["case_id"], 0) + 1

    def uid(label: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"openadapt-qualify:{campaign_id}:{label}"))

    payload = QualificationAdmissionPayload.model_validate(
        {
            "admission_id": uid(f"admission:{result_sha256(result)}"),
            "tenant_id": uid("tenant"),
            "workflow_id": uid("workflow"),
            "workflow_version_id": uid(
                f"workflow-version:{result['workflow_contract_sha256']}"
            ),
            "bundle_version_id": uid(f"bundle:{bundle_content_digest}"),
            "runtime_validation_id": uid("runtime-validation"),
            "bundle_artifact_sha256": _hex(bundle_content_digest, "bundle-artifact"),
            "bundle_content_digest": _hex(bundle_content_digest, "bundle"),
            "governed_authorization_template_sha256": _hex(
                None, "authorization-template"
            ),
            "application_contract_sha256": _hex(None, "application"),
            "substrate_contract_sha256": _hex(None, "substrate"),
            "environment_contract_sha256": _hex(
                result.get("environment_contract_sha256"), "environment"
            ),
            "runtime_environment_sha256": _hex(None, "runtime-environment"),
            "runtime_contract_sha256": _hex(None, "runtime"),
            "input_policy_sha256": _hex(None, "input-policy"),
            "action_policy_sha256": _hex(None, "action-policy"),
            "network_policy_sha256": _hex(None, "network-policy"),
            "identity_contract_sha256": _hex(identity_contract_sha256, "identity"),
            "effect_contract_sha256": _hex(effect_contract_sha256, "effect"),
            "operator_contract_sha256": _hex(None, "operator"),
            "campaign": {
                "artifact_sha256": hashlib.sha256(result_bytes).hexdigest(),
                "contract_sha256": _hex(result["workflow_contract_sha256"], "workflow"),
                "outcomes_sha256": result_sha256(result),
                "oracle_id": oracle_id,
                "oracle_contract_sha256": _hex(
                    result["verify_config_sha256"], "verify"
                ),
                "tasks": [
                    {
                        "task": campaign_id,
                        "condition": case_id,
                        "required_trials": trials_required,
                        "observed_trials": count,
                    }
                    for case_id, count in sorted(observed.items())
                ],
                "failure_taxonomy": sorted(
                    {"over_halt", "silent_incorrect_success"}
                    | {c["fault_class"] for c in result["cases"] if c["fault_class"]}
                ),
                "decision": "admitted",
            },
            "issuer": {
                "key_id": LOCAL_DEV_KEY_ID,
                "workflow": LOCAL_DEV_ISSUER_WORKFLOW,
                "ref": f"{LOCAL_DEV_REF_PREFIX}qualify",
            },
            "issued_at": _utc(clock),
            "not_before": _utc(clock),
            "expires_at": _utc(clock + timedelta(days=7)),
        }
    )
    return sign_production_shaped_local_admission(payload)


__all__ = [
    "EXPECTED_VERDICT",
    "RESULT_SCHEMA",
    "build_result",
    "case_entry",
    "local_admission",
    "observed_verdict",
    "result_sha256",
    "verifier_identity_of",
    "verify_config_sha256",
]
