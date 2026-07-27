"""The local Run Receipt: a shareable artifact built from an ALLOW-LIST.

A run's ``REPORT.md`` is the operator's artifact and is deliberately rich: it
embeds a before/after screenshot per step, the plaintext values that were
typed, the OCR text harvested for identity checks, resolved coordinates, and
the run's parameters.  Every one of those is a PHI or secret carrier.  Sharing
one from a live system of record is a reportable breach, which is exactly what
:class:`openadapt_flow.privacy.PlaintextPHIWarning` warns about.

Therefore the shareable artifact is **generated additively from a closed
allow-list, never redacted subtractively from the operator artifact.**
Subtractive redaction of a run report is unwinnable: burned-in pixels, OCR text
that was captured *precisely because* it identifies a record, and free-form
halt reasons all leak, and one missed field is a breach.

The shape follows the reviewed precedent already shipped in
``openadapt-types`` 0.6.0's portable human-decision contract, which permits only
closed enums, counts, digests, opaque identifiers, expiry, and allowed actions,
with no screenshot, OCR text, raw value, or free-form field.

Nothing outside :class:`RunReceipt` may serialize.  The model is
``extra="forbid"``, so an unknown key is REFUSED rather than silently dropped,
and every field is either a closed enum, a bounded count, a digest, or a
strictly validated package version. There is no operator-supplied text field.

Explicitly forbidden and structurally unrepresentable here: screenshots, OCR
text, typed values, parameters, URLs, hostnames, coordinates, application name,
organization name, user name, workflow name, step intents, and halt free text.

The receipt is a LOCAL file.  Nothing in this module performs network I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_flow.ir import RunReport
from openadapt_flow.verification import VerificationTier

#: Schema identifier carried by every emitted receipt.
RECEIPT_SCHEMA = "openadapt.run-receipt/v2"

#: Terminal execution outcomes a receipt may state.
ReceiptOutcome = Literal["VERIFIED"]

#: Closed terminal transaction class (``openadapt_flow.transaction``).
ReceiptTransactionOutcome = Literal["VERIFIED"]

#: Resolution rungs the ladder may report (mirrors the hosted rail's closed set).
ReceiptRung = Literal[
    "structural",
    "template",
    "template_global",
    "geometry",
    "ocr",
    "grounder",
    "api",
]

#: Evidence classes the outcome envelope may carry.
ReceiptEvidenceClass = Literal[
    "authorization",
    "identity",
    "postcondition",
    "effect_tier_1",
    "effect_tier_2",
    "effect_tier_3",
    "effect_tier_4",
    "model",
    "compensation",
]

#: Weakest effect evidence used to prove any required effect.  Reporting the
#: weakest tier prevents a strong verifier on one effect from masking weaker
#: evidence on another.
ReceiptEffectTier = Literal[
    "independent_system",
    "independent_session",
    "persisted_state_reacquisition",
]

ReceiptSubstrate = Literal["web", "windows", "macos", "linux", "rdp", "citrix"]

#: ``synthetic-tutorial`` marks a run against the bundled synthetic application,
#: which contains no real data BY CONSTRUCTION.  Anything else is ``production``
#: and is subject to the sanitize/approve egress flow before it may be published.
ReceiptProvenance = Literal["synthetic-tutorial", "production"]

_TIER_NAMES: dict[int, ReceiptEffectTier] = {
    int(VerificationTier.INDEPENDENT_SYSTEM): "independent_system",
    int(VerificationTier.INDEPENDENT_SESSION): "independent_session",
    int(VerificationTier.PERSISTED_STATE_REACQUISITION): (
        "persisted_state_reacquisition"
    ),
}

ReceiptProfile = Literal["standard", "regulated"]
ReceiptNetworkObservation = Literal["none", "observed"]
ReceiptCount = Annotated[int, Field(ge=0)]

_EVIDENCE_CLASS_BY_SOURCE = {
    "structured": "application_structured_text",
    "identifier_region": "recorded_and_live_region",
    "captured_context": "captured_context_ocr",
    "application": "application_identity",
    "session": "session_identity",
    "workflow_state": "workflow_state_identity",
    "api_parameter": "api_request_effect_binding",
}

_FLOW_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-(?:a|b|rc)[0-9]+)?$")
_HOUR_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:00:00Z$")


class ReceiptError(ValueError):
    """A receipt could not be built or written."""


class RunReceipt(BaseModel):
    """The complete, closed set of fields a shareable receipt may contain.

    ``extra="forbid"``: an unknown key is refused, not dropped.  This is the
    load-bearing property -- the receipt is safe because nothing outside this
    declaration can reach it, not because something was removed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.run-receipt/v2"] = "openadapt.run-receipt/v2"

    #: This is the success receipt. Halts remain in the private run evidence.
    outcome: ReceiptOutcome
    transaction_outcome: ReceiptTransactionOutcome
    profile: ReceiptProfile
    production_eligible: Literal[True]

    steps_total: ReceiptCount
    steps_ok: ReceiptCount
    heals: ReceiptCount
    model_calls: ReceiptCount
    est_cost_usd: float = Field(ge=0.0)
    duration_ms: ReceiptCount

    rung_histogram: dict[ReceiptRung, ReceiptCount] = Field(default_factory=dict)
    evidence_classes: list[ReceiptEvidenceClass] = Field(default_factory=list)
    effect_tier_reached: ReceiptEffectTier
    authorization_required: ReceiptCount
    authorization_confirmed: ReceiptCount
    identity_required: ReceiptCount
    identity_confirmed: ReceiptCount
    postconditions_required: ReceiptCount
    postconditions_confirmed: ReceiptCount
    effects_required: int = Field(ge=1)
    effects_confirmed: int = Field(ge=1)

    #: Identity-armed steps over identity-applicable steps, as counts only.
    identity_armed: ReceiptCount
    identity_applicable: ReceiptCount

    #: The counter-metric, always present: consequential steps this run stopped
    #: on even though the independent verifier had CONFIRMED the effect.  A
    #: receipt that reports successes without reporting over-halts is worth
    #: nothing, so this field is not optional.
    over_halt_count: Literal[0]

    substrate: ReceiptSubstrate
    provenance: ReceiptProvenance
    receipt_builder_version: str = Field(pattern=_FLOW_VERSION_RE.pattern)
    external_network_calls: ReceiptNetworkObservation

    #: Exact content digest of the bundle that ran.  This is what makes the
    #: receipt checkable: a third party can run the same public bundle and
    #: compare digests.  Verifiability is the point.
    bundle_digest: str = Field(pattern="^[a-f0-9]{64}$")
    #: SHA-256 over the canonical JSON of every OTHER field.  Set by
    #: :func:`build_receipt`; a caller never supplies it.
    receipt_digest: str = Field(pattern="^[a-f0-9]{64}$")

    #: Truncated to the hour, UTC.  Minute/second resolution is a correlation
    #: handle that a receipt does not need.
    generated_at: str = Field(pattern=_HOUR_UTC_RE.pattern)

    @model_validator(mode="after")
    def _complete_verified_contract(self) -> "RunReceipt":
        pairs = (
            (
                "authorization",
                self.authorization_required,
                self.authorization_confirmed,
            ),
            ("identity", self.identity_required, self.identity_confirmed),
            (
                "postcondition",
                self.postconditions_required,
                self.postconditions_confirmed,
            ),
            ("effect", self.effects_required, self.effects_confirmed),
        )
        for name, required, confirmed in pairs:
            if confirmed != required:
                raise ValueError(
                    f"VERIFIED receipt requires complete {name} coverage: "
                    f"{confirmed}/{required}"
                )
        if self.authorization_required != 1:
            raise ValueError(
                "VERIFIED receipt requires exactly one governed authorization"
            )
        if self.postconditions_required < 1:
            raise ValueError("VERIFIED receipt requires postcondition evidence")
        if self.identity_armed != self.identity_applicable:
            raise ValueError("VERIFIED receipt requires complete identity arming")
        if self.steps_ok != self.steps_total:
            raise ValueError("VERIFIED receipt requires every executed step to succeed")
        if len(self.evidence_classes) != len(set(self.evidence_classes)):
            raise ValueError("receipt evidence classes must be unique")
        if "authorization" not in self.evidence_classes:
            raise ValueError("VERIFIED receipt lacks authorization evidence")
        if self.identity_required and "identity" not in self.evidence_classes:
            raise ValueError("VERIFIED receipt lacks identity evidence")
        if (
            self.postconditions_required
            and "postcondition" not in self.evidence_classes
        ):
            raise ValueError("VERIFIED receipt lacks postcondition evidence")
        if not any(item.startswith("effect_tier_") for item in self.evidence_classes):
            raise ValueError("VERIFIED receipt lacks effect evidence")
        if "effect_tier_4" in self.evidence_classes:
            raise ValueError("immediate-screen evidence cannot produce VERIFIED")
        if (self.model_calls > 0) != ("model" in self.evidence_classes):
            raise ValueError("model-call count and evidence class disagree")
        if self.model_calls > 0 and self.external_network_calls != "observed":
            raise ValueError("model calls require observed external network calls")
        expected = hashlib.sha256(self.canonical_json(include_digest=False)).hexdigest()
        if self.receipt_digest != expected:
            raise ValueError("receipt digest does not match its canonical payload")
        return self

    def canonical_json(self, *, include_digest: bool = True) -> bytes:
        """Deterministic bytes for hashing and for the publish diff."""

        payload = self.model_dump(mode="json", exclude_none=True)
        if not include_digest:
            payload.pop("receipt_digest", None)
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")


def _hour_utc(value: Optional[str]) -> str:
    """Parse ``value`` and truncate to the hour in UTC."""

    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ReceiptError(
                "run report started_at is not an ISO-8601 timestamp"
            ) from exc
    else:
        raise ReceiptError("run report has no started_at timestamp")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        parsed.astimezone(timezone.utc)
        .replace(minute=0, second=0, microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _weakest_confirmed_tier(report: Any) -> ReceiptEffectTier:
    """Weakest tier among CONFIRMED effects; every contract must clear it."""

    weakest: Optional[int] = None
    for result in report.results:
        for evidence in result.effect_evidence:
            if evidence.final_verdict != "confirmed":
                continue
            tier = evidence.verification_tier
            if tier is None or int(tier) not in _TIER_NAMES:
                raise ReceiptError(
                    "VERIFIED receipt found effect evidence below the "
                    "independent verification floor"
                )
            weakest = int(tier) if weakest is None else max(weakest, int(tier))
    if weakest is None:
        raise ReceiptError(
            "VERIFIED receipt requires every effect at independent-system, "
            "independent-session, or persisted-state strength"
        )
    return _TIER_NAMES[weakest]  # type: ignore[return-value]


def _over_halt_count(report: Any) -> int:
    """Steps this run stopped on although the effect was CONFIRMED.

    An over-halt is a refusal that cost the operator a completed task without
    preventing a wrong write: the independent verifier had already confirmed
    the declared effect, and the run halted anyway.  Counting it here keeps the
    receipt honest -- the same discipline the safety claims are measured by.
    """

    count = 0
    for result in report.results:
        halted = bool(result.safety_halt) or result.failure_category in {
            "governed_refusal",
            "safety_halt",
        }
        if not halted:
            continue
        if any(
            evidence.final_verdict == "confirmed" for evidence in result.effect_evidence
        ):
            count += 1
    return count


def build_receipt(report: Any) -> RunReceipt:
    """Build a production-provenance receipt from a retained run report.

    A deserialized report cannot prove that it came from the bundled synthetic
    tutorial: ``governed_approval_source`` is deliberately only descriptive.
    The synthetic provenance rail is therefore private to the live tutorial
    orchestration path below; generic callers and ``report-run`` always emit a
    production-provenance receipt.
    """

    return _build_receipt(report, provenance="production")


def _build_tutorial_receipt(report: Any) -> RunReceipt:
    """Build the bundled tutorial's receipt inside its live orchestration path."""

    return _build_receipt(report, provenance="synthetic-tutorial")


def _build_receipt(
    report: Any,
    *,
    provenance: ReceiptProvenance,
) -> RunReceipt:
    """Project a :class:`~openadapt_flow.ir.RunReport` onto the allow-list.

    Every value is read from a typed field of the report.  No free text, no
    parameter, no image path, no URL, and no workflow name is consulted.

    Args:
        report: The completed :class:`~openadapt_flow.ir.RunReport`.
        provenance: Internal provenance selected by the live tutorial or the
            public production builder. Never inferred from report free text.
    Returns:
        The receipt, with :attr:`RunReceipt.receipt_digest` bound to the
        canonical JSON of every other field.

    Raises:
        ReceiptError: the report carries no classified execution outcome, so
            there is no outcome the receipt could truthfully state.
    """

    # RunReport is mutable after construction, and model_copy/model_construct
    # can also bypass nested validators.  The receipt is a publication boundary,
    # so rebuild the complete typed envelope through JSON before trusting any
    # field.  This additionally prevents arbitrary look-alike objects from
    # supplying a hand-picked subset of report attributes.
    try:
        encoded_report = report.model_dump_json()
        report = RunReport.model_validate_json(encoded_report)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReceiptError("run report failed typed JSON revalidation") from exc

    outcome = report.execution_outcome
    envelope = getattr(report, "outcome_envelope", None)
    profile = getattr(report, "execution_profile", None)
    transaction_outcome = getattr(report, "transaction_outcome", None)
    bundle_digest = getattr(report, "bundle_content_digest", None)
    minimum_effect_tier = getattr(report, "governed_minimum_effect_tier", None)
    if not (
        outcome == "VERIFIED"
        and bool(getattr(report, "success", False))
        and bool(getattr(report, "production_eligible", False))
        and bool(getattr(report, "execution_completed", False))
        and profile in {"standard", "regulated"}
        and transaction_outcome == "VERIFIED"
        and getattr(report, "transaction_billable", None) is True
        and getattr(report, "transaction_platform_fault", None) is False
        and envelope is not None
        and bundle_digest is not None
        and getattr(report, "governed_authorization_id", None)
        and getattr(report, "governed_runtime_inputs_digest", None)
        and getattr(report, "governed_policy_contract_sha256", None)
        and minimum_effect_tier in {1, 2, 3}
    ):
        raise ReceiptError(
            "run does not carry the complete governed VERIFIED contract "
            "(production profile, authorization, transaction, envelope, and "
            "bundle binding are all required)"
        )

    if (
        provenance == "synthetic-tutorial"
        and getattr(report, "governed_approval_source", None)
        != "openadapt-flow-tutorial"
    ):
        raise ReceiptError(
            "synthetic-tutorial provenance requires the retained tutorial "
            "authorization source"
        )

    evidence_classes = sorted(envelope.evidence_classes)
    required = envelope.required_contracts
    passed = envelope.passed_contracts
    if int(required.authorization) != 1 or int(passed.authorization) != 1:
        raise ReceiptError(
            "VERIFIED receipt requires exactly one passed authorization contract"
        )

    # Approved-unverified writes are useful in Demo, but no representation of
    # that approval may coexist with a shareable VERIFIED receipt.  Check every
    # retained mirror: report list, authorization mapping, result, and journal.
    if (
        report.approved_unverified_effect_step_ids
        or report.governed_authorized_effect_contracts
        or any(result.effect_approved_unverified for result in report.results)
        or any(entry.approved_unverified for entry in report.effect_journal)
    ):
        raise ReceiptError(
            "VERIFIED receipt cannot retain an approved-unverified write"
        )

    required_identity_ids = set(getattr(report, "required_identity_step_ids", []))
    if len(required_identity_ids) != len(
        getattr(report, "required_identity_step_ids", [])
    ):
        raise ReceiptError("required identity step ids must be unique")
    executed_identity_results = [
        result
        for result in report.results
        if not result.skipped
        and not result.exception_handled
        and result.step_id in required_identity_ids
    ]
    if len(executed_identity_results) != int(required.identity) or any(
        result.identity is None or result.identity.status != "verified"
        for result in executed_identity_results
    ):
        raise ReceiptError(
            "VERIFIED receipt requires exact retained identity evidence coverage"
        )
    if report.identity_unarmed:
        raise ReceiptError("VERIFIED receipt cannot retain an unarmed identity step")
    if int(report.identity_armed_steps) != int(report.identity_applicable_steps) or int(
        report.identity_applicable_steps
    ) < len(required_identity_ids):
        raise ReceiptError(
            "VERIFIED receipt requires complete workflow identity arming"
        )

    for result in executed_identity_results:
        identity = result.identity
        assert identity is not None  # guarded by the complete-coverage check
        if identity.mode != "signal_quorum":
            if (
                identity.coverage <= 0
                or not identity.expected.strip()
                or not identity.observed.strip()
            ):
                raise ReceiptError(
                    "VERIFIED receipt found empty retained identity evidence"
                )
            continue
        signals = [item.signal for item in identity.signal_evidence]
        sources = [item.source for item in identity.signal_evidence]
        verified_signals = sum(
            item.verdict == "verified" for item in identity.signal_evidence
        )
        if (
            not signals
            or len(signals) != len(set(signals))
            or len(sources) != len(set(sources))
            or any(
                item.evidence_class != _EVIDENCE_CLASS_BY_SOURCE[item.source]
                for item in identity.signal_evidence
            )
            or any(item.verdict == "conflict" for item in identity.signal_evidence)
            or identity.quorum_required is None
            or identity.quorum_verified is None
            or identity.quorum_required > len(signals)
            or identity.quorum_verified != verified_signals
            or verified_signals < identity.quorum_required
        ):
            raise ReceiptError(
                "VERIFIED receipt found inconsistent signal-quorum identity evidence"
            )

    if any(
        not result.ok
        or result.safety_halt
        or result.failure_category is not None
        or result.postconditions_ok is False
        for result in report.results
    ):
        raise ReceiptError(
            "VERIFIED receipt found a failed, halted, or refuted retained step"
        )
    postcondition_results = sum(
        result.postconditions_ok is True for result in report.results
    )
    if int(required.postcondition) > 0 and postcondition_results == 0:
        raise ReceiptError("VERIFIED receipt requires retained postcondition evidence")
    if int(required.postcondition) < postcondition_results:
        raise ReceiptError(
            "retained postcondition evidence exceeds the VERIFIED envelope"
        )

    declared_effects: Counter[str] = Counter()
    confirmed_effects: Counter[str] = Counter()
    observed_effect_classes: set[str] = set()
    for result in report.results:
        if result.effect_contract_hashes and result.effect_verified is not True:
            raise ReceiptError(
                "VERIFIED receipt requires effect_verified=true for every "
                "declared effect"
            )
        result_declared_effects = Counter(result.effect_contract_hashes)
        result_confirmed_effects: Counter[str] = Counter()
        declared_effects.update(result_declared_effects)
        for evidence in result.effect_evidence:
            if not (
                evidence.initial_verdict == "confirmed"
                and evidence.final_verdict == "confirmed"
            ):
                raise ReceiptError(
                    "VERIFIED receipt found effect evidence that was not cleanly "
                    "confirmed"
                )
            if evidence.observed_effect != "present":
                raise ReceiptError(
                    "VERIFIED receipt requires every confirmed effect to be "
                    "observed present"
                )
            if evidence.reconciliation_completed or evidence.reconciliation_actions:
                raise ReceiptError(
                    "VERIFIED receipt cannot use reconciled effect evidence"
                )
            tier = evidence.verification_tier
            if tier is None or int(tier) not in _TIER_NAMES:
                raise ReceiptError(
                    "VERIFIED receipt found effect evidence below the "
                    "independent verification floor"
                )
            if not VerificationTier(int(tier)).satisfies(
                VerificationTier(int(minimum_effect_tier))
            ):
                raise ReceiptError(
                    "VERIFIED receipt effect evidence is weaker than the "
                    "governed authorization minimum"
                )
            confirmed_effects[evidence.effect_contract_hash] += 1
            result_confirmed_effects[evidence.effect_contract_hash] += 1
            observed_effect_classes.add(f"effect_tier_{int(tier)}")
        if result_declared_effects != result_confirmed_effects:
            raise ReceiptError(
                "VERIFIED receipt requires exact per-step effect-hash coverage"
            )
    if not declared_effects or declared_effects != confirmed_effects:
        raise ReceiptError(
            "VERIFIED receipt requires exact declared/evidence effect-hash coverage"
        )
    if sum(confirmed_effects.values()) != int(passed.effect):
        raise ReceiptError(
            "effect evidence cardinality disagrees with the VERIFIED envelope"
        )
    envelope_effect_classes = {
        item for item in evidence_classes if item.startswith("effect_tier_")
    }
    if envelope_effect_classes != observed_effect_classes:
        raise ReceiptError(
            "retained effect evidence tiers disagree with the VERIFIED envelope"
        )
    if envelope.compensation_actions or "compensation" in evidence_classes:
        raise ReceiptError(
            "VERIFIED receipt cannot retain compensation or reconciliation evidence"
        )

    from openadapt_flow.transaction import _attempt_state, _is_consequential_result

    effect_result_positions = [
        index
        for index, result in enumerate(report.results)
        if result.effect_contract_hashes or result.effect_evidence
    ]
    transaction_result_positions = [
        index
        for index, result in enumerate(report.results)
        if not result.skipped and _is_consequential_result(result)
    ]
    if transaction_result_positions != effect_result_positions:
        raise ReceiptError(
            "VERIFIED receipt requires declared effects for every "
            "transaction-consequential result"
        )
    effect_results = [report.results[index] for index in effect_result_positions]
    transaction_results = [
        report.results[index] for index in transaction_result_positions
    ]
    journal = list(getattr(report, "effect_journal", []))
    if len(journal) != len(transaction_results):
        raise ReceiptError(
            "VERIFIED receipt requires one retained transaction journal entry "
            "per transaction-consequential step"
        )
    if any(
        result.step_id not in required_identity_ids
        or result.postconditions_ok is not True
        for result in effect_results
    ):
        raise ReceiptError(
            "VERIFIED receipt requires identity and postcondition coverage for "
            "every effect-bearing step"
        )

    for result, entry in zip(transaction_results, journal):
        expected_attempt = _attempt_state(result)
        if expected_attempt == "not_actuated":
            raise ReceiptError(
                "VERIFIED receipt cannot claim an effect for a non-actuated step"
            )
        uncertainty = result.delivery_uncertainty
        if expected_attempt == "delivery_uncertain":
            if not (
                uncertainty is not None
                and uncertainty.verification_attempted
                and uncertainty.postconditions_confirmed is True
                and uncertainty.effects_confirmed is True
                and uncertainty.resolved_by_contract
            ):
                raise ReceiptError(
                    "VERIFIED receipt found unresolved action-delivery uncertainty"
                )
            expected_observed_at = uncertainty.observed_at
        else:
            expected_observed_at = None
        if not (
            entry.step_id == result.step_id
            and entry.consequential is True
            and entry.intended_effect_contract_hashes == result.effect_contract_hashes
            and entry.attempt_state == expected_attempt
            and entry.observed_effect == "present"
            and entry.effect_verified is True
            and entry.approved_unverified is False
            and entry.verification_performed is True
            and entry.observed_at == expected_observed_at
            and entry.collateral_reconciliation_actions == 0
        ):
            raise ReceiptError(
                "VERIFIED receipt transaction journal disagrees with retained "
                "effect evidence"
            )

    rungs: dict[str, int] = {}
    for rung, count in dict(report.rung_counts).items():
        if rung not in ReceiptRung.__args__:  # type: ignore[attr-defined]
            raise ReceiptError(f"report contains an unknown resolution rung: {rung!r}")
        rungs[rung] = int(count)

    over_halts = _over_halt_count(report)
    if over_halts:
        raise ReceiptError("VERIFIED receipt cannot retain an over-halt")
    substrate = getattr(report, "execution_target_kind", None)
    if substrate not in ReceiptSubstrate.__args__:  # type: ignore[attr-defined]
        raise ReceiptError("VERIFIED receipt requires a recognized execution substrate")

    try:
        payload = dict(
            schema_version=RECEIPT_SCHEMA,
            outcome=outcome,
            transaction_outcome=transaction_outcome,
            profile=profile,
            production_eligible=True,
            steps_total=len(report.results),
            steps_ok=sum(1 for result in report.results if result.ok),
            heals=int(report.heal_count),
            model_calls=int(report.model_calls),
            est_cost_usd=round(float(report.est_model_cost_usd), 6),
            duration_ms=int(round(float(report.total_ms))),
            rung_histogram=rungs,  # type: ignore[arg-type]
            evidence_classes=evidence_classes,  # type: ignore[arg-type]
            effect_tier_reached=_weakest_confirmed_tier(report),
            authorization_required=int(required.authorization),
            authorization_confirmed=int(passed.authorization),
            identity_required=int(required.identity),
            identity_confirmed=int(passed.identity),
            postconditions_required=int(required.postcondition),
            postconditions_confirmed=int(passed.postcondition),
            effects_required=int(required.effect),
            effects_confirmed=int(passed.effect),
            identity_armed=int(report.identity_armed_steps),
            identity_applicable=int(report.identity_applicable_steps),
            over_halt_count=0,
            substrate=cast(ReceiptSubstrate, substrate),
            provenance=provenance,
            receipt_builder_version=_receipt_builder_version(),
            external_network_calls=envelope.external_network_calls,
            bundle_digest=bundle_digest,
            generated_at=_hour_utc(getattr(report, "started_at", None)),
        )
    except ValueError as exc:
        raise ReceiptError(f"run cannot produce a VERIFIED receipt: {exc}") from exc
    digest = hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    try:
        return RunReceipt.model_validate({**payload, "receipt_digest": digest})
    except ValueError as exc:
        raise ReceiptError(f"run cannot produce a VERIFIED receipt: {exc}") from exc


def _receipt_builder_version() -> str:
    from openadapt_flow import __version__

    return __version__


def render_receipt_markdown(receipt: RunReceipt) -> str:
    """Render the receipt as a small, postable Markdown card.

    Reads only :class:`RunReceipt` -- it cannot widen the allow-list.
    """

    identity = f"{receipt.identity_armed} of {receipt.identity_applicable}"
    effects = f"{receipt.effects_confirmed} of {receipt.effects_required}"
    ladder = (
        ", ".join(
            f"{rung} {count}" for rung, count in sorted(receipt.rung_histogram.items())
        )
        or "none recorded"
    )
    lines = [
        f"# {receipt.outcome} — OpenAdapt run receipt",
        "",
    ]
    lines += [
        "| | |",
        "|---|---|",
        f"| outcome | `{receipt.outcome}` |",
        f"| transaction | `{receipt.transaction_outcome}` |",
        f"| profile | `{receipt.profile}` |",
        f"| steps | {receipt.steps_ok} of {receipt.steps_total} ok |",
        f"| model calls | **{receipt.model_calls}** |",
        f"| est. model cost | ${receipt.est_cost_usd:.6f} |",
        f"| duration | {receipt.duration_ms} ms |",
        f"| heals | {receipt.heals} |",
        f"| effects confirmed | {effects} |",
        f"| weakest effect evidence | `{receipt.effect_tier_reached}` |",
        f"| identity-armed steps | {identity} |",
        f"| over-halts | {receipt.over_halt_count} |",
        f"| resolution ladder | {ladder} |",
        f"| substrate | `{receipt.substrate}` |",
        f"| provenance | `{receipt.provenance}` |",
        f"| receipt builder version | {receipt.receipt_builder_version} |",
        f"| external network calls | `{receipt.external_network_calls}` |",
    ]
    lines += [
        f"| bundle digest | `{receipt.bundle_digest}` |",
        f"| receipt digest | `{receipt.receipt_digest}` |",
        f"| generated | {receipt.generated_at} |",
        "",
        "Run the same public bundle yourself and compare the bundle digest.",
        "",
        "This receipt is generated from a closed allow-list. It contains no "
        "screenshot, no OCR text, no typed value, no parameter, no URL, no "
        "hostname, no coordinate, and no free-form halt reason.",
    ]
    return "\n".join(lines) + "\n"


_CARD_BG = (16, 18, 22)
_CARD_FG = (233, 236, 241)
_CARD_MUTED = (140, 148, 160)
_CARD_OK = (86, 204, 132)
_CARD_WARN = (232, 176, 84)


def _receipt_rows(receipt: RunReceipt) -> list[tuple[str, str]]:
    """The rendered rows, derived ONLY from :class:`RunReceipt` fields."""

    ladder = (
        "  ".join(
            f"{rung} {count}" for rung, count in sorted(receipt.rung_histogram.items())
        )
        or "none recorded"
    )
    rows = [
        ("transaction", receipt.transaction_outcome),
        ("profile", receipt.profile),
        ("steps", f"{receipt.steps_ok} of {receipt.steps_total} ok"),
        ("model calls", str(receipt.model_calls)),
        ("est. model cost", f"${receipt.est_cost_usd:.6f}"),
        ("duration", f"{receipt.duration_ms} ms"),
        ("heals", str(receipt.heals)),
        (
            "effects confirmed",
            f"{receipt.effects_confirmed} of {receipt.effects_required}",
        ),
        ("effect evidence", receipt.effect_tier_reached),
        (
            "identity-armed",
            f"{receipt.identity_armed} of {receipt.identity_applicable}",
        ),
        ("over-halts", str(receipt.over_halt_count)),
        ("ladder", ladder),
        ("substrate", str(receipt.substrate)),
        ("provenance", receipt.provenance),
        ("receipt builder", receipt.receipt_builder_version),
    ]
    rows += [
        ("bundle digest", (receipt.bundle_digest or "")[:16] or "unbound"),
        ("receipt digest", (receipt.receipt_digest or "")[:16] or "unbound"),
        ("generated", receipt.generated_at),
    ]
    return rows


def render_receipt_png(receipt: RunReceipt) -> Any:
    """Render the receipt as a small dark card (a Pillow ``Image``).

    Reads only :class:`RunReceipt`, so it cannot widen the allow-list.
    """

    from PIL import Image, ImageDraw, ImageFont

    def font(size: int) -> Any:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # pragma: no cover - very old Pillow
            return ImageFont.load_default()

    title_font = font(34)
    label_font = font(15)
    value_font = font(15)
    foot_font = font(12)

    rows = _receipt_rows(receipt)
    pad = 28
    row_h = 24
    width = 620
    height = pad + 48 + len(rows) * row_h + pad + 30

    image = Image.new("RGB", (width, height), _CARD_BG)
    draw = ImageDraw.Draw(image)
    accent = _CARD_OK if receipt.outcome == "VERIFIED" else _CARD_WARN
    draw.rectangle([(0, 0), (6, height)], fill=accent)

    y = pad
    draw.text((pad, y), receipt.outcome, font=title_font, fill=accent)
    y += 44
    for key, value in rows:
        draw.text((pad, y), key, font=label_font, fill=_CARD_MUTED)
        draw.text((pad + 190, y), value, font=value_font, fill=_CARD_FG)
        y += row_h

    draw.text(
        (pad, height - pad - 4),
        "no screenshot, no OCR text, no typed value, no parameter, no URL",
        font=foot_font,
        fill=_CARD_MUTED,
    )
    return image


def write_receipt(receipt: RunReceipt, out_dir: Path | str) -> dict[str, Path]:
    """Write ``receipt.json``, ``receipt.md``, and ``receipt.png`` locally.

    Purely local: this function performs no network I/O and never uploads.
    """

    try:
        receipt = RunReceipt.model_validate(receipt.model_dump(mode="json"))
    except ValueError as exc:
        raise ReceiptError(f"receipt failed integrity validation: {exc}") from exc
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "receipt.json"
    json_path.write_bytes(receipt.canonical_json() + b"\n")
    md_path = directory / "receipt.md"
    md_path.write_text(render_receipt_markdown(receipt), encoding="utf-8")
    png_path = directory / "receipt.png"
    render_receipt_png(receipt).save(png_path, format="PNG")
    return {"json": json_path, "markdown": md_path, "png": png_path}
