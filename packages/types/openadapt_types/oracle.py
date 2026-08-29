"""Partner oracle adapter: one read, a tier, a production Seal gate.

This module is the public interface. It does not contain per-system-of-record
recipes, thresholds, or connector credentials. A partner implements
``channel`` and ``read``. Flow consumes the same contract later.

Seal ladder (higher is stronger):

* 0 visual / OCR. Local development. Never a production ``VERIFIED``.
* 1 second session / independent UI read. Not a production Seal.
* 2 system-of-record read (API, DB, file, ack).
* 3 counterparty artifact (payer status, legal export).

This numbering is the Seal ladder. It is not
``EffectVerificationTier``, which counts the other way.
"""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import Mapping, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictStr,
    model_validator,
)

PRODUCTION_SEAL_MINIMUM_TIER = 2


class OracleTier(IntEnum):
    """Seal oracle ladder. Higher is stronger. Production Seals require 2 or 3."""

    VISUAL = 0
    INDEPENDENT_SESSION = 1
    SYSTEM_OF_RECORD = 2
    COUNTERPARTY = 3


class OracleChannel(str, Enum):
    """How the oracle read the effect. The channel, not the payload, sets the tier."""

    VISUAL = "visual"
    OCR = "ocr"
    SECOND_SESSION = "second_session"
    API = "api"
    DB = "db"
    FILE = "file"
    ACK = "ack"
    COUNTERPARTY = "counterparty"


_CHANNEL_TIER: Mapping[OracleChannel, OracleTier] = {
    OracleChannel.VISUAL: OracleTier.VISUAL,
    OracleChannel.OCR: OracleTier.VISUAL,
    OracleChannel.SECOND_SESSION: OracleTier.INDEPENDENT_SESSION,
    OracleChannel.API: OracleTier.SYSTEM_OF_RECORD,
    OracleChannel.DB: OracleTier.SYSTEM_OF_RECORD,
    OracleChannel.FILE: OracleTier.SYSTEM_OF_RECORD,
    OracleChannel.ACK: OracleTier.SYSTEM_OF_RECORD,
    OracleChannel.COUNTERPARTY: OracleTier.COUNTERPARTY,
}

_EFFECT_STRENGTH_TO_TIER: Mapping[str, OracleTier] = {
    "independent_system_of_record": OracleTier.SYSTEM_OF_RECORD,
    "independent_session": OracleTier.INDEPENDENT_SESSION,
}


class ProductionSealRefused(ValueError):
    """Raised when a production ``VERIFIED`` stamp is requested below tier 2."""


class OracleObservation(BaseModel):
    """One read-only observation. The channel decides the tier."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: OracleChannel
    identity: dict[str, StrictStr] = Field(min_length=1)
    value: dict[str, JsonValue]

    @model_validator(mode="after")
    def _identity_keys_are_present(self) -> "OracleObservation":
        if any(not key for key in self.identity):
            raise ValueError("oracle identity keys must be non-empty")
        return self

    @property
    def tier(self) -> OracleTier:
        return _CHANNEL_TIER[self.channel]


@runtime_checkable
class OracleAdapter(Protocol):
    """Read-only effect check. Implement ``channel`` and ``read``.

    Tier-2 channels are ``api``, ``db``, ``file``, and ``ack``.
    ``second_session`` uses the same adapter and classifies as tier 1.
    """

    channel: OracleChannel

    def read(self, identity: Mapping[str, str]) -> OracleObservation: ...


Tier2Oracle = OracleAdapter


def tier_of(channel: OracleChannel) -> OracleTier:
    """Return the Seal tier for one channel."""

    return _CHANNEL_TIER[channel]


def oracle_tier_from_effect_strength(strength: object | None) -> OracleTier:
    """Map an Execute ``EffectStrengthV1`` value onto the Seal ladder."""

    name = getattr(strength, "value", strength)
    if not isinstance(name, str):
        return OracleTier.VISUAL
    return _EFFECT_STRENGTH_TO_TIER.get(name, OracleTier.VISUAL)


def production_seal_allowed(tier: OracleTier | int) -> bool:
    """True when ``tier`` may stamp a production ``VERIFIED`` Seal."""

    return int(tier) >= PRODUCTION_SEAL_MINIMUM_TIER


def refuse_production_verified(tier: OracleTier | int) -> None:
    """Raise if ``tier`` cannot mint production ``VERIFIED``."""

    if not production_seal_allowed(tier):
        raise ProductionSealRefused(
            "a verified outcome requires oracle tier 2 or 3"
        )


def issue_production_verified(observation: OracleObservation) -> OracleTier:
    """Return ``observation.tier`` if it may stamp production ``VERIFIED``.

    Visual / OCR (tier 0) and second-session UI (tier 1) raise
    ``ProductionSealRefused``. The payload cannot upgrade the channel.
    """

    refuse_production_verified(observation.tier)
    return observation.tier
