"""The verifier adapter platform: one stable interface every effect verifier
implements, plus the plugin seam that lets a customer ship their own.

This module FORMALIZES the contract the existing substrates (REST / FHIR /
SQL / file / document-hash / onscreen) already follow, so a new adapter --
first-party or a customer plugin -- targets a named, versioned surface instead
of imitating whichever built-in it read last. Nothing here changes runtime
behavior of the existing verifiers; it names it.

Lifecycle (every adapter)::

    configure          -> the adapter's __init__ (or a registered factory
                          called with the deployment's EffectsConfig + the
                          governed run params). Secrets arrive as env-var /
                          secret-manager REFERENCES (AuthRef, *_env fields),
                          never literals, and are resolved here -- failing
                          LOUD when absent. Entity + tenant binding uses the
                          same ``ValueExpr({param: ...})`` form the SQL
                          verifier proved, resolved at construction so the
                          adapter probes the record THIS run writes.
    test-connection    -> :meth:`VerifierAdapter.test_connection`: a read-only
                          reachability probe (never a write) an operator or
                          preflight can run before any consequential action.
    capture-before     -> :meth:`VerifierAdapter.capture_pre_state`: snapshot
                          the system of record (the baseline for delta /
                          duplicate / collateral accounting).
    capture-after      -> :meth:`VerifierAdapter.capture_post_state`: a fresh
                          post-action snapshot (default: same read as before).
    verdict            -> :meth:`VerifierAdapter.verify`: poll-until-settled
                          within the effect's deadline, judge with the shared
                          judge (``_common.judge_records``), and return a
                          fail-safe :class:`EffectVerdict`.

Guarantees the interface encodes (and the qualification fixtures assert):

- **Cardinality** is the contract's ``expected_count`` -- zero (absence),
  exactly-one (at-most-once), or exact-N -- judged identically everywhere.
- **Duplicate detection** is ``count_new_only`` + ``idempotency_key``;
  **collateral-effect detection** is ``forbid_collateral_loss`` plus the
  :func:`apply_collateral_hooks` seam for substrate-specific checks.
- **Settlement**: verification polls the system of record until the effect is
  CONFIRMED or the deadline (``Effect.timeout_s``) lapses -- an asynchronous
  write that never settles is a failure, not a pass
  (:func:`poll_until_settled`).
- **Freshness**: an adapter that can read record timestamps may enforce a
  freshness window (:func:`enforce_freshness`); data outside the window is
  STALE -> never a silent pass.
- **Explicit non-pass results**: :func:`classify_adapter_result` refines the
  three-valued verdict into CONFIRMED / REFUTED / UNAVAILABLE / STALE /
  CONFLICTING / INDETERMINATE, and :func:`transaction_outcome_for` maps every
  non-confirmed result onto the transaction taxonomy (uncertain or
  conflicting -> ``RECONCILIATION_REQUIRED``; affirmatively-absent ->
  ``HALTED_BEFORE_EFFECT``). There is no mapping to a silent success.
- **Evidence minimization**: :class:`RedactionPolicy` /
  :func:`redact_verdict` apply field-level redaction to the evidence an
  adapter emits (matched records, observed values) without weakening the
  verdict itself.

Confidence tiers (why "the screen said so" is not proof)
--------------------------------------------------------

Every adapter advertises a :class:`~openadapt_flow.verification.VerificationTier`
(lower = stronger). :func:`confidence_label` names them:

- tier 1 ``independent-system``: a read through the system of record's own
  API/DB/store -- independent proof.
- tier 2 ``independent-session``: same application, separately authenticated
  read-only session -- independent-ish, weaker isolation.
- tier 3 ``reacquired-state``: the app's own UI re-navigated to re-fetch
  persisted state (different-path screen read-back).
- tier 4 ``screen-consistency``: same-surface screen read-back. This is a
  LOWER-CONFIDENCE CONSISTENCY CHECK, never independent system-of-record
  proof -- an optimistic UI can paint success while nothing persisted. The
  onscreen adapter declares ``independent_system_of_record = False`` and
  execution profiles gate on the tier, not on prose.

Plugin SDK seam
---------------

A customer package implements the same interface and registers a factory
under the entry-point group :data:`ENTRY_POINT_GROUP` (or programmatically via
:func:`register_verifier_factory`); ``deployment.build_effect_verifier``
resolves any non-builtin ``effects.kind`` through this registry. See
``docs/EFFECT_KIT.md`` ("Shipping your own adapter") and the worked example
plugin in ``tests/example_verifier_plugin.py``.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
    Verdict,
)
from openadapt_flow.verification import VerificationTier

#: Entry-point group a customer verifier package registers its factory under.
#: Each entry point's NAME is the ``effects.kind`` it serves; its value loads
#: to a :data:`VerifierFactory`.
ENTRY_POINT_GROUP = "openadapt_flow.effect_verifiers"

#: A registered adapter constructor: ``factory(cfg, params) -> verifier``.
#: ``cfg`` is the deployment's ``EffectsConfig`` (the factory reads its own
#: fields, including secret env-var REFERENCES); ``params`` are the governed
#: run parameters for ``ValueExpr({param: ...})`` binding. A factory must fail
#: LOUD (raise ``ValueError``) on missing config or secrets -- never construct
#: a silently broken verifier.
VerifierFactory = Callable[[Any, Optional[Mapping[str, str]]], Any]


class ConnectionProbe(BaseModel):
    """Result of the read-only ``test-connection`` lifecycle step.

    ``ok=False`` means the adapter cannot currently read its system of record
    -- every verdict it would produce right now is UNAVAILABLE, so a preflight
    should refuse to start the consequential run.
    """

    ok: bool
    substrate: str
    reason: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class AdapterResult(str, Enum):
    """Fine-grained adapter result, refining the three-valued verdict.

    Every value except CONFIRMED is a halt; the refinement exists so the
    OPERATOR knows which failure mode they are reconciling, not so any of
    them can be softened into a pass.
    """

    #: Effect present, correct, and (when enforced) fresh -> proceed.
    CONFIRMED = "confirmed"
    #: The system of record affirmatively shows NO matching effect
    #: (observed count zero) -> the write did not land.
    REFUTED = "refuted"
    #: The system of record could not be read at all (transport/auth failure)
    #: -> nothing can be claimed, including absence.
    UNAVAILABLE = "unavailable"
    #: Data was read but is outside the declared freshness window -> the read
    #: cannot certify the CURRENT state.
    STALE = "stale"
    #: A record WAS written but contradicts the contract (duplicate, wrong
    #: value, collateral loss) -> a business effect occurred and must be
    #: reconciled.
    CONFLICTING = "conflicting"
    #: Could not certify for a reason other than unreachability (e.g. an
    #: unreadable pre-state baseline under ``count_new_only``).
    INDETERMINATE = "indeterminate"


def classify_adapter_result(verdict: EffectVerdict) -> AdapterResult:
    """Refine an :class:`EffectVerdict` into an :class:`AdapterResult`.

    Pure and total: every verdict classifies, and only a CONFIRMED verdict
    classifies as CONFIRMED (never a silent pass through refinement).
    """
    if verdict.verdict is Verdict.CONFIRMED:
        return AdapterResult.CONFIRMED
    if verdict.stale:
        return AdapterResult.STALE
    if verdict.verdict is Verdict.INDETERMINATE:
        if verdict.unavailable:
            return AdapterResult.UNAVAILABLE
        return AdapterResult.INDETERMINATE
    # REFUTED: split affirmative absence from an effect that DID occur but is
    # wrong (duplicate / wrong value / collateral) -- same split as
    # EffectVerdict.observed_effect.
    if verdict.observed_effect == "conflicting":
        return AdapterResult.CONFLICTING
    return AdapterResult.REFUTED


def reconciliation_required(result: AdapterResult) -> bool:
    """Whether ``result`` demands reconciliation before any retry.

    UNAVAILABLE / STALE / CONFLICTING / INDETERMINATE all mean "a business
    effect may exist in an unknown or contradicted state": the runtime must
    not blind-retry the consequential write.
    """
    return result in (
        AdapterResult.UNAVAILABLE,
        AdapterResult.STALE,
        AdapterResult.CONFLICTING,
        AdapterResult.INDETERMINATE,
    )


def transaction_outcome_for(result: AdapterResult) -> Optional[Any]:
    """Map an adapter result onto the terminal transaction taxonomy.

    Returns ``None`` for CONFIRMED (the step proceeds; run-level ``VERIFIED``
    is decided by ``transaction.classify_transaction_outcome``). Every other
    result maps to a halting outcome -- there is deliberately NO mapping to a
    success:

    - REFUTED (affirmatively absent) -> ``HALTED_BEFORE_EFFECT``;
    - UNAVAILABLE / STALE / CONFLICTING / INDETERMINATE ->
      ``RECONCILIATION_REQUIRED`` (uncertain or contradicted persistence;
      never claim "no effect", never blind-retry).
    """
    from openadapt_flow.transaction import TransactionOutcome

    if result is AdapterResult.CONFIRMED:
        return None
    if result is AdapterResult.REFUTED:
        return TransactionOutcome.HALTED_BEFORE_EFFECT
    return TransactionOutcome.RECONCILIATION_REQUIRED


def confidence_label(tier: VerificationTier) -> str:
    """Human-stable name for a verification tier (docs / evidence surfaces)."""
    return {
        VerificationTier.INDEPENDENT_SYSTEM: "independent-system",
        VerificationTier.INDEPENDENT_SESSION: "independent-session",
        VerificationTier.PERSISTED_STATE_REACQUISITION: "reacquired-state",
        VerificationTier.IMMEDIATE_SCREEN: "screen-consistency",
    }[tier]


# -- evidence minimization ----------------------------------------------------


class RedactionPolicy(BaseModel):
    """Field-level evidence-minimization policy for adapter verdicts.

    Applied to the EVIDENCE an adapter emits (matched records, observed /
    expected values) -- never to the verdict itself, which stays fail-safe.
    ``redact_fields`` names record fields whose values must not leave the
    verifier (PHI/PII); ``keep_fields``, when set, is an allowlist -- every
    field NOT named is redacted. ``hash_redacted`` replaces a redacted value
    with a one-way SHA-256 digest (so an auditor can still compare two runs'
    evidence for equality) instead of a bare marker.
    """

    redact_fields: list[str] = Field(default_factory=list)
    keep_fields: Optional[list[str]] = None
    # A plain digest is reversible for low-entropy values such as dates of
    # birth, postal codes, and short identifiers. Keep evidence opaque by
    # default. Deployments that deliberately need equality correlation may
    # opt into hashing after reviewing that disclosure risk.
    hash_redacted: bool = False

    def is_redacted(self, field: str) -> bool:
        if self.keep_fields is not None and field not in self.keep_fields:
            return True
        return field in self.redact_fields

    def redact_value(self, value: Any) -> str:
        if not self.hash_redacted:
            return "[redacted]"
        digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
        return f"[redacted sha256:{digest}]"


def redact_verdict(
    verdict: EffectVerdict,
    policy: Optional[RedactionPolicy],
    *,
    field: Optional[str] = None,
) -> EffectVerdict:
    """Return ``verdict`` with its evidence minimized per ``policy``.

    ``field`` is the effect's read-back field name, so a policy that redacts
    it also redacts the verdict's ``observed_value`` / ``expected_value``.
    The verdict enum, counts, and reason are never altered -- redaction
    minimizes evidence, it cannot soften a failure into a pass.
    """
    if policy is None:
        return verdict
    records = [
        {
            k: (policy.redact_value(v) if policy.is_redacted(k) else v)
            for k, v in record.items()
        }
        for record in verdict.matched_records
    ]
    update: dict[str, Any] = {"matched_records": records}
    if field is not None and policy.is_redacted(field):
        if verdict.observed_value is not None:
            update["observed_value"] = policy.redact_value(verdict.observed_value)
        if verdict.expected_value is not None:
            update["expected_value"] = policy.redact_value(verdict.expected_value)
    return verdict.model_copy(update=update)


# -- settlement + freshness ---------------------------------------------------


def poll_until_settled(
    fetch: Callable[[], Optional[list[dict[str, Any]]]],
    effect: Effect,
    before: EffectState,
    *,
    substrate: str,
    poll_interval_s: float = 0.2,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> EffectVerdict:
    """The shared asynchronous-settlement loop every adapter uses.

    Re-reads the system of record until the effect judges CONFIRMED or the
    effect's deadline (``Effect.timeout_s``) lapses, then returns the FINAL
    judgement -- a write that never settles inside the window is a failure
    verdict (settlement timeout), never a pass. ``clock`` / ``sleep`` are
    injectable so qualification fixtures exercise the deadline without
    real waiting.
    """
    deadline = clock() + max(0.0, effect.timeout_s)
    while True:
        verdict = judge_records(effect, before, fetch(), substrate=substrate)
        if verdict.confirmed or clock() >= deadline:
            return verdict
        sleep(poll_interval_s)


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse a record timestamp: ISO 8601 (``Z`` ok) or numeric epoch seconds.

    Returns ``None`` -- read as "no usable timestamp" -- for anything else;
    the freshness check then rules STALE (a record whose freshness cannot be
    read is never assumed fresh).
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def enforce_freshness(
    verdict: EffectVerdict,
    *,
    freshness_field: str,
    window_s: float,
    now: Optional[datetime] = None,
) -> EffectVerdict:
    """Demote a CONFIRMED verdict to STALE when its evidence is out of window.

    Reads ``freshness_field`` from each matched record; a record with a
    missing/unparseable timestamp or one older than ``window_s`` seconds makes
    the verdict INDETERMINATE with ``stale=True`` (the read cannot certify the
    CURRENT state of the record). Non-confirmed verdicts pass through
    unchanged -- freshness can only weaken evidence, never rescue a failure.
    """
    if verdict.verdict is not Verdict.CONFIRMED or not verdict.matched_records:
        return verdict
    now_dt = now if now is not None else datetime.now(timezone.utc)
    stale = 0
    for record in verdict.matched_records:
        ts = parse_timestamp(record.get(freshness_field))
        age_s = (now_dt - ts).total_seconds() if ts is not None else None
        # Reject evidence too far in the future as well as too old. A bounded
        # amount of clock skew (the configured window) remains acceptable.
        if ts is None or age_s is None or age_s > window_s or age_s < -window_s:
            stale += 1
    if stale == 0:
        return verdict
    return verdict.model_copy(
        update={
            "verdict": Verdict.INDETERMINATE,
            "stale": True,
            "reason": (
                f"{stale} matched record(s) have a {freshness_field!r} "
                f"timestamp missing or older than the {window_s:.0f}s "
                "freshness window -- STALE read, cannot certify the current "
                "state; HALT (never a silent pass on stale data)"
            ),
        }
    )


#: A substrate-specific collateral-effect check: given the before and after
#: snapshots, return a human-readable violation reason, or ``None`` when
#: clean. Runs AFTER the shared judge; any violation demotes a CONFIRMED
#: verdict to REFUTED (conflicting).
CollateralHook = Callable[[EffectState, EffectState], Optional[str]]


def apply_collateral_hooks(
    verdict: EffectVerdict,
    before: EffectState,
    after: EffectState,
    hooks: tuple[CollateralHook, ...] | list[CollateralHook],
) -> EffectVerdict:
    """Run substrate-specific collateral/duplicate hooks over a verdict.

    The built-in collateral machinery (``forbid_collateral_loss``,
    ``count_new_only``) lives in the shared judge; these hooks are the seam
    for checks only a substrate can express (e.g. "no message left the outbox
    to any OTHER recipient"). A hook can only demote -- a violation turns a
    CONFIRMED verdict REFUTED; it never upgrades a failure.
    """
    if verdict.verdict is not Verdict.CONFIRMED:
        return verdict
    reasons = [r for hook in hooks if (r := hook(before, after)) is not None]
    if not reasons:
        return verdict
    return verdict.model_copy(
        update={
            "verdict": Verdict.REFUTED,
            "reason": "collateral-effect hook violation: " + "; ".join(reasons),
        }
    )


# -- the adapter interface ----------------------------------------------------


@runtime_checkable
class VerifierAdapter(Protocol):
    """The stable adapter surface (superset of ``EffectVerifier``).

    ``configure`` is the constructor / registered factory; the remaining
    lifecycle is methods. Implementations should subclass
    :class:`VerifierAdapterBase` to inherit the default ``test_connection`` /
    ``capture_post_state`` and the demotion-safe helpers, but any object with
    this surface is accepted (the protocol is structural).
    """

    substrate: str
    verification_tier: VerificationTier

    def test_connection(self, context: Any = None) -> ConnectionProbe: ...

    def capture_pre_state(self, context: Any = None) -> EffectState: ...

    def capture_post_state(self, context: Any = None) -> EffectState: ...

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict: ...


class VerifierAdapterBase:
    """Base class formalizing the adapter lifecycle for implementers.

    Subclasses set :attr:`substrate` and :attr:`verification_tier`, implement
    :meth:`capture_pre_state` (one fresh read of the system of record) and
    :meth:`verify`, and inherit:

    - :meth:`test_connection` -- read-only reachability probe (never raises;
      any exception reads as unreachable);
    - :meth:`capture_post_state` -- a fresh post-action snapshot (default:
      the same read as the pre-state capture).
    """

    #: Stable substrate name for audit.
    substrate: str = ""
    #: Evidence strength this adapter's CONFIRMED verdict carries.
    verification_tier: VerificationTier = VerificationTier.INDEPENDENT_SYSTEM
    #: False for adapters whose read shares the actuation surface (screen
    #: read-back): their CONFIRMED is a consistency signal, NOT independent
    #: system-of-record proof. Execution profiles gate on the tier; this flag
    #: makes the demotion explicit in code.
    independent_system_of_record: bool = True

    def capture_pre_state(self, context: Any = None) -> EffectState:
        raise NotImplementedError

    def capture_post_state(self, context: Any = None) -> EffectState:
        """A fresh post-action snapshot (same read as the pre-state)."""
        return self.capture_pre_state(context)

    def test_connection(self, context: Any = None) -> ConnectionProbe:
        """Read-only reachability probe; safe to run before any action."""
        try:
            state = self.capture_pre_state(context)
        except Exception as exc:  # noqa: BLE001 - probe must never raise
            return ConnectionProbe(
                ok=False,
                substrate=self.substrate,
                reason=f"connection probe raised: {exc!r}",
            )
        return ConnectionProbe(
            ok=state.reachable,
            substrate=state.substrate or self.substrate,
            reason=(
                "system of record reachable"
                if state.reachable
                else "system of record unreachable or unreadable"
            ),
            detail=dict(state.detail),
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        raise NotImplementedError


class RedactingVerifier:
    """Protocol-transparent wrapper applying a :class:`RedactionPolicy`.

    Wraps any adapter: every verdict's evidence is minimized on the way out
    (matched records, observed/expected values); everything else -- including
    substrate, tier, ``bind_backend``, ``verification_tier_for`` -- delegates
    to the wrapped adapter unchanged. Built by
    ``deployment.build_effect_verifier`` when the config sets
    ``evidence_redact_fields`` / ``evidence_keep_fields``.
    """

    def __init__(self, inner: Any, policy: RedactionPolicy) -> None:
        self._inner = inner
        self._policy = policy

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def test_connection(self, context: Any = None) -> ConnectionProbe:
        probe = getattr(self._inner, "test_connection", None)
        if callable(probe):
            result = probe(context)
            if isinstance(result, ConnectionProbe):
                return result
        state = self._inner.capture_pre_state(context)
        return ConnectionProbe(
            ok=state.reachable,
            substrate=state.substrate,
            reason="" if state.reachable else "unreachable",
        )

    def capture_pre_state(self, context: Any = None) -> EffectState:
        return self._inner.capture_pre_state(context)

    def capture_post_state(self, context: Any = None) -> EffectState:
        post = getattr(self._inner, "capture_post_state", None)
        if callable(post):
            return post(context)
        return self._inner.capture_pre_state(context)

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        verdict = self._inner.verify(expected, before, context)
        return redact_verdict(verdict, self._policy, field=expected.field)


# -- plugin registry ----------------------------------------------------------

_REGISTRY: dict[str, VerifierFactory] = {}
_ENTRY_POINTS_LOADED = False


def register_verifier_factory(
    kind: str, factory: VerifierFactory, *, replace: bool = False
) -> None:
    """Register ``factory`` to build verifiers for ``effects.kind == kind``.

    The programmatic half of the plugin seam (the declarative half is the
    :data:`ENTRY_POINT_GROUP` entry point). A built-in kind cannot be shadowed
    (``build_effect_verifier`` consults built-ins first), and re-registering a
    kind without ``replace=True`` fails loud.
    """
    key = kind.strip().lower()
    if not key:
        raise ValueError("verifier kind must be a non-empty string")
    if key in _REGISTRY and not replace:
        raise ValueError(
            f"a verifier factory for kind {key!r} is already registered "
            "(pass replace=True to override)"
        )
    _REGISTRY[key] = factory


def _load_entry_point_factories() -> None:
    """Load every :data:`ENTRY_POINT_GROUP` entry point into the registry.

    A plugin that fails to import raises (fail loud): a deployment naming a
    plugin kind must never silently fall through to "unknown kind".
    """
    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return
    import importlib.metadata

    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        name = ep.name.strip().lower()
        if name in _REGISTRY:
            continue  # programmatic registration wins
        try:
            loaded = ep.load()
        except Exception as exc:
            raise ValueError(
                f"effect-verifier plugin entry point {ep.name!r} "
                f"({ep.value}) failed to load: {exc!r}"
            ) from exc
        _REGISTRY[name] = loaded
    _ENTRY_POINTS_LOADED = True


def resolve_verifier_factory(kind: str) -> Optional[VerifierFactory]:
    """The registered factory for ``kind`` (programmatic or entry point).

    Returns ``None`` when no plugin serves the kind -- the caller then fails
    loud with the unknown-kind error.
    """
    _load_entry_point_factories()
    return _REGISTRY.get(kind.strip().lower())


def registered_kinds() -> tuple[str, ...]:
    """Currently registered plugin kinds (diagnostics / docs)."""
    _load_entry_point_factories()
    return tuple(sorted(_REGISTRY))
