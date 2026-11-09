"""Named policy packs so certify is one flag, not a lawyer session.

Packs do not lower gates. They name which shipped policy, minimum effect
tier, and signer purpose the operator is asking for. A missing identity or
effect pin still HALTs the proposal. A local-dev signer still cannot enter a
production trust map.
"""

from __future__ import annotations

from typing import Final, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.verification import VerificationTier

PolicyPackName = Literal["community", "cloud", "regulated"]


class PolicyPack(BaseModel):
    """One closed pack. Mechanism is public; it points at a shipped policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: PolicyPackName
    policy_name: Literal["permissive", "clinical-write"]
    minimum_effect_tier: VerificationTier
    allow_local_dev_signer: bool
    require_identity_on_writes: bool
    require_system_of_record_effect: bool
    summary: str = Field(min_length=1, max_length=256)


COMMUNITY_PACK: Final[PolicyPack] = PolicyPack(
    name="community",
    policy_name="permissive",
    minimum_effect_tier=VerificationTier.PERSISTED_STATE_REACQUISITION,
    allow_local_dev_signer=True,
    require_identity_on_writes=True,
    require_system_of_record_effect=True,
    summary="Local and MockMed. Local-dev admission only. Same identity and effect pins.",
)

CLOUD_PACK: Final[PolicyPack] = PolicyPack(
    name="cloud",
    policy_name="clinical-write",
    minimum_effect_tier=VerificationTier.INDEPENDENT_SESSION,
    allow_local_dev_signer=False,
    require_identity_on_writes=True,
    require_system_of_record_effect=True,
    summary="Hosted browser runs. Production signer required. clinical-write policy.",
)

REGULATED_PACK: Final[PolicyPack] = PolicyPack(
    name="regulated",
    policy_name="clinical-write",
    minimum_effect_tier=VerificationTier.INDEPENDENT_SYSTEM,
    allow_local_dev_signer=False,
    require_identity_on_writes=True,
    require_system_of_record_effect=True,
    summary="Independent system-of-record verifier. Production signer required.",
)

POLICY_PACKS: Final[Mapping[str, PolicyPack]] = {
    pack.name: pack for pack in (COMMUNITY_PACK, CLOUD_PACK, REGULATED_PACK)
}


def load_policy_pack(name: str) -> PolicyPack:
    """Return one named pack, or raise ValueError."""

    pack = POLICY_PACKS.get(name)
    if pack is None:
        known = ", ".join(sorted(POLICY_PACKS))
        raise ValueError(f"unknown policy pack {name!r}; known packs: {known}")
    return pack
