"""How much of a pause's context a delivery path is permitted to carry.

The engine already grades *evidence* with
:class:`~openadapt_flow.verification.VerificationTier` — weaker evidence is
recorded as weaker rather than concealed. Delivery of an attended decision gets
the same treatment, for the same reason: an operator who answered a question
without seeing what broke made a different decision from one who did, and the
receipt should be able to say which.

The ladder, strongest first (lower value = more context, mirroring
``VerificationTier``):

``LOCAL_FULL`` (1)
    The local ``presentation`` block: protected screenshot crops, the gated
    control label, the whole halt detail. Served by the loopback console and,
    through the runner-local decision portal, to a phone on the customer's own
    network. This is the only tier that carries pixels.

``REMOTE_CLOSED_CONTEXT`` (2)
    The signed PHI-free task *plus*
    :class:`~openadapt_flow.console.decision_context.RemoteHaltContextV1` — what
    broke, which resolution rungs were tried and what each returned, and which
    contracts a continuation re-proves. Every field is a closed enum, a bounded
    integer, or a boolean. **There is no string field and no image.** A
    third-party relay carrying this tier is structurally incapable of
    representing protected content, exactly as it is at tier 3.

``REMOTE_IDENTIFIERS`` (3)
    The signed task alone: opaque ids, digests, counts, closed enums, expiry.
    An operator can see that a decision is waiting and what class it is, but not
    what happened. This was the only remote tier before this module existed.

``NOTIFICATION_ONLY`` (4)
    A count and a fixed sentence: "something needs you, open the console."
    Carries no task, so nothing can be answered from it.

What is deliberately **not** a rung
-----------------------------------

A "scrubbed" tier — run the local presentation through a PII/PHI detector and
send the result — is not defined here, because defining it would imply it is
available. It is not implemented, and
``docs/DECISION_DELIVERY.md`` records why: it would replace a structural
guarantee ("the envelope cannot represent protected content") with a
statistical one ("a recall-limited detector caught everything"), which is the
exact substitution this engine exists to refuse. If it is ever built it belongs
between tiers 2 and 3, and ``regulated`` must refuse it.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Optional, Union


class DecisionDeliveryTier(IntEnum):
    """How much pause context a delivery path may carry.

    Lower values carry more. ``satisfies`` therefore reads the same way as
    :meth:`openadapt_flow.verification.VerificationTier.satisfies`: a tier
    satisfies a ceiling when it is no *stronger* than the ceiling permits.
    """

    LOCAL_FULL = 1
    REMOTE_CLOSED_CONTEXT = 2
    REMOTE_IDENTIFIERS = 3
    NOTIFICATION_ONLY = 4

    @property
    def carries_protected_evidence(self) -> bool:
        """Whether this tier may carry pixels or gated free text at all."""
        return self is DecisionDeliveryTier.LOCAL_FULL

    @property
    def is_remote(self) -> bool:
        """Whether this tier may leave the runner's trust boundary."""
        return self is not DecisionDeliveryTier.LOCAL_FULL

    def satisfies(self, ceiling: "DecisionDeliveryTier") -> bool:
        """Whether this tier is permitted where ``ceiling`` is the maximum."""
        return int(self) >= int(ceiling)


#: Configuration and profile spellings, so a deployment never writes an int.
_NAMES = {tier.name.lower(): tier for tier in DecisionDeliveryTier}


def resolve_delivery_tier(
    value: Union[DecisionDeliveryTier, str, int, None],
    *,
    default: Optional[DecisionDeliveryTier] = None,
) -> DecisionDeliveryTier:
    """Resolve a tier name, or fail loudly on an unknown value.

    Args:
        value: A :class:`DecisionDeliveryTier`, its lowercase name, its integer
            value, or ``None``.
        default: Returned when ``value`` is ``None``. Required in that case;
            there is deliberately no implicit default, because "how much
            context may leave the runner" is never a silent choice.

    Raises:
        ValueError: For an unknown value, or a ``None`` with no default.
    """
    if value is None:
        if default is None:
            raise ValueError("a decision delivery tier must be named explicitly")
        return default
    if isinstance(value, DecisionDeliveryTier):
        return value
    if isinstance(value, bool):  # bool is an int; never a tier
        raise ValueError(f"unknown decision delivery tier {value!r}")
    if isinstance(value, int):
        try:
            return DecisionDeliveryTier(value)
        except ValueError as exc:
            raise ValueError(f"unknown decision delivery tier {value!r}") from exc
    resolved = _NAMES.get(str(value).strip().lower())
    if resolved is None:
        choices = ", ".join(sorted(_NAMES))
        raise ValueError(
            f"unknown decision delivery tier {value!r}; expected one of: {choices}"
        )
    return resolved


def effective_remote_tier(
    configured: Union[DecisionDeliveryTier, str, int, None],
    ceiling: DecisionDeliveryTier,
) -> DecisionDeliveryTier:
    """The tier a remote projection actually carries.

    Two independent limits apply and the *weaker* one wins: what the deployment
    asked for, and what the active execution profile permits. Neither can widen
    the other, so a profile ceiling cannot be escaped by configuration and a
    deployment that wants less context always gets less.

    ``LOCAL_FULL`` is refused outright — it is not a remote tier, and accepting
    it here would be the exact "quietly put decision content where the PHI
    stance says it cannot go" failure this ladder exists to prevent.

    Args:
        configured: The deployment's requested tier, or ``None`` to take the
            ceiling as-is.
        ceiling: The active execution profile's maximum remote tier.

    Raises:
        ValueError: If ``configured`` names ``local_full`` or an unknown tier.
    """
    resolved = resolve_delivery_tier(configured, default=ceiling)
    if resolved is DecisionDeliveryTier.LOCAL_FULL:
        raise ValueError(
            "local_full is not a remote decision delivery tier; protected "
            "evidence never leaves the runner's trust boundary"
        )
    return max(resolved, ceiling)
