"""Pure candidate model. No native transport or runtime consumer is provided."""

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

PROTOTYPE_CONTRACT = "openadapt.native-capability-source-prototype/v1"
S_OK = 0
UIA_E_NOTSUPPORTED = -2147220988  # SDK 0x80040204 as signed HRESULT.


class Pattern(Enum):
    TOGGLE = (10015, "IUIAutomationTogglePattern")
    SELECTION_ITEM = (10010, "IUIAutomationSelectionItemPattern")
    VALUE = (10002, "IUIAutomationValuePattern")


class Capability(Enum):
    SUPPORTED = "supported"
    EXPLICITLY_UNSUPPORTED = "explicitly_unsupported"
    UNKNOWN = "unknown"


class Output(Enum):
    NULL = "null"
    TYPED = "typed"
    INVALID = "invalid"


class Stage(Enum):
    BEFORE_ACQUIRE = "before_acquire"
    BEFORE_QUERY_INTERFACE = "before_query_interface"
    AFTER_QUERY_INTERFACE = "after_query_interface"
    AFTER_RELEASE = "after_release"
    AFTER_UNSUPPORTED = "after_unsupported"


class Reason(Enum):
    SUPPORTED = "supported"
    EXPLICITLY_UNSUPPORTED = "explicitly_unsupported"
    CONTRACT = "contract"
    SCOPE_OR_GUARD = "scope_or_guard"
    INVALID_REPLY = "invalid_reply"
    CONTRADICTORY_OUTPUT = "contradictory_output"
    NORMAL_NULL = "normal_null"
    ACQUISITION = "acquisition"
    QUERY_INTERFACE = "query_interface"
    TRANSPORT = "transport"
    RELEASE = "release"


@dataclass(frozen=True)
class Scope:
    """Opaque non-content identity supplied by an eventual qualified binder."""

    identity: bytes
    generation: int

    def valid(self) -> bool:
        return (
            type(self.identity) is bytes
            and len(self.identity) == 32
            and type(self.generation) is int
            and 0 <= self.generation < 2**64
        )


@dataclass(frozen=True)
class Checkpoint:
    scope: Scope
    # Model of a supported raw password property, not a convenience bool.
    password_status: object


@dataclass(frozen=True)
class RawReply:
    """Synthetic operation outcome; not a wire proof or a native pointer."""

    hresult: object
    output: Output

    def valid(self) -> bool:
        return (
            type(self.hresult) is int
            and -(2**31) <= self.hresult < 2**31
            and type(self.output) is Output
        )


@dataclass(frozen=True)
class Observation:
    pattern: Pattern
    scope: Scope
    capability: Capability
    reason: Reason


class SyntheticTransport(Protocol):
    """Owned synthetic slots. Implementations do not expose pointer addresses."""

    def acquire(self, pattern: Pattern) -> RawReply: ...

    def query_exact_interface(self, pattern: Pattern) -> RawReply: ...

    def release(self, slot: str) -> None: ...


def observe_model(
    pattern: Pattern,
    scope: Scope,
    *,
    contract: str,
    checkpoint: Callable[[Stage], Checkpoint | None],
    transport: SyntheticTransport,
) -> Observation:
    """Exercise the candidate call order without creating native authority.

    Release is mandatory ownership cleanup, including after refusal. A refusal
    never triggers another checkpoint/getter to investigate its cause.
    """
    capability = Capability.UNKNOWN
    reason = Reason.CONTRACT
    acquired = False
    queried = False

    def allowed(stage: Stage) -> bool:
        try:
            current = checkpoint(stage)
            return (
                type(current) is Checkpoint
                and type(current.scope) is Scope
                and current.scope.valid()
                and current.scope == scope
                and current.password_status is False
            )
        except Exception:
            return False

    if (
        type(pattern) is not Pattern
        or type(scope) is not Scope
        or not scope.valid()
        or type(contract) is not str
        or contract != PROTOTYPE_CONTRACT
    ):
        return Observation(pattern, scope, capability, reason)
    if not allowed(Stage.BEFORE_ACQUIRE):
        return Observation(pattern, scope, capability, Reason.SCOPE_OR_GUARD)
    try:
        reply = transport.acquire(pattern)
        acquired = type(reply) is RawReply and reply.output is Output.TYPED
        if type(reply) is not RawReply or not reply.valid():
            reason = Reason.INVALID_REPLY
        else:
            if reply.hresult == UIA_E_NOTSUPPORTED:
                if reply.output is not Output.NULL:
                    reason = Reason.CONTRADICTORY_OUTPUT
                elif not allowed(Stage.AFTER_UNSUPPORTED):
                    reason = Reason.SCOPE_OR_GUARD
                else:
                    capability = Capability.EXPLICITLY_UNSUPPORTED
                    reason = Reason.EXPLICITLY_UNSUPPORTED
            elif reply.hresult != S_OK:
                reason = Reason.ACQUISITION
            elif reply.output is Output.NULL:
                reason = Reason.NORMAL_NULL
            elif reply.output is not Output.TYPED:
                reason = Reason.INVALID_REPLY
            elif not allowed(Stage.BEFORE_QUERY_INTERFACE):
                reason = Reason.SCOPE_OR_GUARD
            else:
                exact = transport.query_exact_interface(pattern)
                queried = type(exact) is RawReply and exact.output is Output.TYPED
                if type(exact) is not RawReply or not exact.valid():
                    reason = Reason.INVALID_REPLY
                else:
                    if exact.hresult != S_OK or exact.output is not Output.TYPED:
                        reason = Reason.QUERY_INTERFACE
                    elif not allowed(Stage.AFTER_QUERY_INTERFACE):
                        reason = Reason.SCOPE_OR_GUARD
                    else:
                        capability = Capability.SUPPORTED
                        reason = Reason.SUPPORTED
    except Exception:
        capability = Capability.UNKNOWN
        reason = Reason.TRANSPORT
    finally:
        # Both owned slots must be cleaned even when the first cleanup fails.
        for owned, slot in ((queried, "queried"), (acquired, "acquired")):
            if owned:
                try:
                    transport.release(slot)
                except Exception:
                    capability = Capability.UNKNOWN
                    reason = Reason.RELEASE
    if capability is Capability.SUPPORTED and not allowed(Stage.AFTER_RELEASE):
        capability = Capability.UNKNOWN
        reason = Reason.SCOPE_OR_GUARD
    return Observation(pattern, scope, capability, reason)


@dataclass(frozen=True)
class CoverageClaim:
    """An input to a model, never an installed/qualified coverage contract."""

    relevant: frozenset[Pattern]
    required: frozenset[Pattern]
    represented: frozenset[Pattern]
    other_relevant_channels: frozenset[str]


@dataclass(frozen=True)
class CoverageAssessment:
    model_consistent: bool
    reason: str


def assess_model_coverage(
    observations: tuple[Observation, ...], scope: Scope, claim: CoverageClaim
) -> CoverageAssessment:
    """Check necessary coverage conditions; emit no field, mask or proof."""
    refused = CoverageAssessment(False, "unrepresented_or_unknown")
    if (
        type(scope) is not Scope
        or not scope.valid()
        or type(claim) is not CoverageClaim
    ):
        return refused
    for values in (claim.relevant, claim.required, claim.represented):
        if type(values) is not frozenset or any(type(p) is not Pattern for p in values):
            return refused
    if type(claim.other_relevant_channels) is not frozenset:
        return refused
    if claim.other_relevant_channels or not claim.required <= claim.relevant:
        return refused
    if not claim.represented <= claim.relevant or type(observations) is not tuple:
        return refused
    seen: dict[Pattern, Observation] = {}
    for observation in observations:
        if (
            type(observation) is not Observation
            or type(observation.pattern) is not Pattern
            or observation.pattern in seen
            or type(observation.scope) is not Scope
            or not observation.scope.valid()
            or observation.scope != scope
            or type(observation.capability) is not Capability
            or type(observation.reason) is not Reason
        ):
            return refused
        expected = {
            Capability.SUPPORTED: Reason.SUPPORTED,
            Capability.EXPLICITLY_UNSUPPORTED: Reason.EXPLICITLY_UNSUPPORTED,
        }.get(observation.capability)
        if expected is None or observation.reason is not expected:
            return refused
        seen[observation.pattern] = observation
    # All three observations are necessary, not a universal state census.
    if set(seen) != set(Pattern):
        return refused
    for pattern, observation in seen.items():
        if observation.capability is Capability.SUPPORTED:
            if pattern not in claim.relevant or pattern not in claim.represented:
                return refused
        elif pattern in claim.required or pattern in claim.represented:
            return refused
    return CoverageAssessment(True, "necessary_model_conditions_only")
