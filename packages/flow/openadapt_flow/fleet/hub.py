"""The fleet hub: verifies contributions, re-signs them, and runs fleet status.

Receivers never see who contributed a patch. The hub:

1. verifies a :class:`~openadapt_flow.fleet.patch.PatchContribution` against
   the pinned fleet roots (``contributor`` purpose);
2. writes the contributor's identity (certificate subject, signer key id,
   serial, intermediate) and the source bundle digests to its internal audit
   log, which is never distributed. That log is how the hub revokes a
   contributor and rolls back everything it contributed;
3. holds the patch until at least :data:`MIN_INDEPENDENT_TENANTS` tenants
   have registered the base application fingerprint (``app_before``), so a
   patch is never the only sign that a particular tenant runs a particular
   version;
4. strips the contribution envelope, truncates ``created_at`` to the hour,
   and signs the payload with its own ``distribution`` key. Every patch the
   hub distributes carries the same hub certificate, so no field links two
   patches to one source.

The hub also signs revocation lists and kill switches with its ``status``
key. It never sees a tenant bundle, never runs a patch, and never decides
promotion on a tenant.

This is a local reference implementation over a directory. A Cloud-hosted
hub would implement the same steps.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Iterable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.fleet._lock import exclusive_lock
from openadapt_flow.fleet.feed import FilePatchFeed
from openadapt_flow.fleet.fingerprint import APP_FAMILY_PATTERN, AppVersionFingerprint
from openadapt_flow.fleet.patch import (
    PatchContribution,
    SignedStructuralPatch,
    sign_patch,
    verify_contribution,
)
from openadapt_flow.fleet.signing import (
    CertificateChain,
    FleetTrust,
    KeyCustody,
    PatchSignatureError,
    sign_document,
    verify_chain,
    verify_document,
)
from openadapt_flow.fleet.status import (
    KillReason,
    KillSwitchPayload,
    RevocationListPayload,
    SignedKillSwitch,
    SignedRevocationList,
    sign_kill_switch,
    sign_revocation_list,
)

#: A patch for an application version is distributed only after this many
#: independent tenants registered that version's fingerprint.
MIN_INDEPENDENT_TENANTS: Final[int] = 3
REGISTRATION_DOMAIN: Final[bytes] = b"openadapt.fleet.fingerprint-registration/v1\x00"
_SHA256 = r"^[a-f0-9]{64}$"


class HubRefused(ValueError):
    """The hub refused a contribution or registration."""


class FingerprintRegistrationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.fleet.fingerprint-registration/v1"] = (
        "openadapt.fleet.fingerprint-registration/v1"
    )
    app_family: str = Field(pattern=APP_FAMILY_PATTERN)
    app_fingerprint_sha256: str = Field(pattern=_SHA256)
    registered_at: str = Field(min_length=20, max_length=64)


class SignedFingerprintRegistration(BaseModel):
    """A tenant says it runs this application version. Sent to the hub only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: FingerprintRegistrationPayload
    signature: str = Field(min_length=1, max_length=128)
    chain: CertificateChain


def register_fingerprint(
    fingerprint: AppVersionFingerprint,
    signer: KeyCustody,
    chain: CertificateChain,
    *,
    now: Optional[datetime] = None,
) -> SignedFingerprintRegistration:
    if chain.signer.payload.purpose != "contributor":
        raise PatchSignatureError("a registration needs a contributor certificate")
    payload = FingerprintRegistrationPayload(
        app_family=fingerprint.app_family,
        app_fingerprint_sha256=fingerprint.fingerprint_sha256(),
        registered_at=(now or datetime.now(timezone.utc))
        .astimezone(timezone.utc)
        .isoformat(),
    )
    return SignedFingerprintRegistration(
        payload=payload,
        signature=sign_document(signer, REGISTRATION_DOMAIN, payload),
        chain=chain,
    )


@dataclass
class HubDecision:
    outcome: Literal["distributed", "held"]
    contribution_id: str
    reason: str
    patch: Optional[SignedStructuralPatch] = None


def _hour(now: datetime) -> str:
    return (
        now.astimezone(timezone.utc)
        .replace(minute=0, second=0, microsecond=0)
        .isoformat()
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".hub-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class FleetHub:
    def __init__(
        self,
        root: Path | str,
        *,
        trust: FleetTrust,
        feed: FilePatchFeed,
        distribution_signer: KeyCustody,
        distribution_chain: CertificateChain,
        status_signer: KeyCustody,
        status_chain: CertificateChain,
        min_tenants: int = MIN_INDEPENDENT_TENANTS,
    ) -> None:
        if min_tenants < MIN_INDEPENDENT_TENANTS:
            raise ValueError(
                f"the distribution rule needs at least {MIN_INDEPENDENT_TENANTS} tenants"
            )
        if distribution_chain.signer.payload.purpose != "distribution":
            raise ValueError(
                "the distribution chain must be a distribution certificate"
            )
        if status_chain.signer.payload.purpose != "status":
            raise ValueError("the status chain must be a status certificate")
        self.root = Path(root)
        self.trust = trust
        self.feed = feed
        self._dist = (distribution_signer, distribution_chain)
        self._status = (status_signer, status_chain)
        self.min_tenants = min_tenants

    # -- internal state (never distributed) ----------------------------------

    @property
    def audit_path(self) -> Path:
        return self.root / "audit" / "contributions.jsonl"

    def _state(self) -> dict[str, Any]:
        path = self.root / "state.json"
        if path.is_file():
            return json.loads(path.read_text())
        return {
            "registrations": {},
            "revoked_key_ids": [],
            "revoked_serials": [],
            "revoked_subjects": [],
            "status_sequence": 0,
        }

    def _save_state(self, state: dict[str, Any]) -> None:
        _write_json(self.root / "state.json", state)

    @property
    def state_lock_path(self) -> Path:
        return self.root / "state.lock"

    def _state_lock(self) -> Any:
        """Hold while reading, changing and saving ``state.json``. Not reentrant."""
        return exclusive_lock(self.state_lock_path)

    def audit_log(self) -> list[dict[str, Any]]:
        if not self.audit_path.is_file():
            return []
        return [
            json.loads(line)
            for line in self.audit_path.read_text().splitlines()
            if line
        ]

    def _audit(self, entry: dict[str, Any]) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")

    # -- registrations -------------------------------------------------------

    def register(
        self,
        registration: SignedFingerprintRegistration,
        *,
        now: Optional[datetime] = None,
    ) -> int:
        """Record that one tenant runs a version. Returns the tenant count."""
        verify_chain(
            registration.chain,
            self.trust,
            purpose="contributor",
            app_family=registration.payload.app_family,
            now=now,
        )
        verify_document(
            registration.chain.signer.payload.signer_public_key,
            REGISTRATION_DOMAIN,
            registration.payload,
            registration.signature,
            "registration",
        )
        subject = registration.chain.signer.payload.subject
        key = registration.payload.app_fingerprint_sha256
        with self._state_lock():
            state = self._state()
            if subject in state["revoked_subjects"]:
                raise HubRefused("this contributor is revoked")
            subjects = set(state["registrations"].get(key, []))
            subjects.add(subject)
            state["registrations"][key] = sorted(subjects)
            self._save_state(state)
            return len(self._independent(state, key))

    @staticmethod
    def _independent(state: dict[str, Any], app_fingerprint_sha256: str) -> list[str]:
        """Registered subjects for a version, excluding revoked contributors."""
        revoked = set(state["revoked_subjects"])
        return [
            subject
            for subject in state["registrations"].get(app_fingerprint_sha256, [])
            if subject not in revoked
        ]

    def tenants_on(self, app_fingerprint_sha256: str) -> int:
        return len(self._independent(self._state(), app_fingerprint_sha256))

    # -- contributions -------------------------------------------------------

    def accept(
        self, contribution: PatchContribution, *, now: Optional[datetime] = None
    ) -> HubDecision:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        verify_contribution(contribution, self.trust, now=current)
        state = self._state()
        subject = contribution.chain.signer.payload.subject
        if subject in state["revoked_subjects"]:
            raise HubRefused("this contributor is revoked")
        base = contribution.payload.patch.app_before.fingerprint_sha256()
        registered = self._independent(state, base)
        if subject not in registered:
            raise HubRefused(
                "the contributor has not registered the base application version"
            )
        cid = contribution.contribution_id
        self._audit(
            {
                "event": "contribution_received",
                "contribution_id": cid,
                "contributor_subject": subject,
                "contributor_signer_key_id": contribution.chain.signer_key_id,
                "contributor_serial": contribution.chain.serial,
                "contributor_intermediate_key_id": contribution.chain.intermediate_key_id,
                "source_prior_bundle_sha256": contribution.payload.source_prior_bundle_sha256,
                "source_bundle_sha256": contribution.payload.source_bundle_sha256,
                "received_at": current.isoformat(),
            }
        )
        held = self.root / "held" / f"{cid}.json"
        _write_json(held, json.loads(contribution.model_dump_json()))
        if len(registered) < self.min_tenants:
            return HubDecision(
                "held",
                cid,
                f"{len(registered)} of {self.min_tenants} tenants registered the "
                "base version; held",
            )
        return self._distribute(contribution, current)

    def _distribute(
        self, contribution: PatchContribution, now: datetime
    ) -> HubDecision:
        signer, chain = self._dist
        payload = contribution.payload.patch.model_copy(
            update={"created_at": _hour(now)}
        )
        patch = sign_patch(payload, signer, chain)
        self.feed.publish(patch)
        cid = contribution.contribution_id
        self._audit(
            {
                "event": "patch_distributed",
                "contribution_id": cid,
                "contributor_subject": contribution.chain.signer.payload.subject,
                "patch_id": patch.patch_id,
                "distributed_at": now.isoformat(),
            }
        )
        held = self.root / "held" / f"{cid}.json"
        if held.exists():
            held.unlink()
        return HubDecision("distributed", cid, "distributed", patch)

    def release_held(self, *, now: Optional[datetime] = None) -> list[HubDecision]:
        """Distribute held patches whose base version now has enough tenants."""
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        out: list[HubDecision] = []
        held_dir = self.root / "held"
        if not held_dir.is_dir():
            return out
        state = self._state()
        for path in sorted(held_dir.glob("*.json")):
            contribution = PatchContribution.model_validate_json(path.read_text())
            if contribution.chain.signer.payload.subject in state["revoked_subjects"]:
                continue
            base = contribution.payload.patch.app_before.fingerprint_sha256()
            if len(self._independent(state, base)) >= self.min_tenants:
                out.append(self._distribute(contribution, current))
        return out

    def contributor_of(self, patch_id: str) -> Optional[str]:
        """Internal lookup for revocation. Receivers have no equivalent."""
        for entry in self.audit_log():
            if (
                entry.get("event") == "patch_distributed"
                and entry["patch_id"] == patch_id
            ):
                return entry["contributor_subject"]
        return None

    # -- fleet status --------------------------------------------------------

    def _next_sequence(self) -> int:
        with self._state_lock():
            state = self._state()
            state["status_sequence"] += 1
            self._save_state(state)
            return state["status_sequence"]

    def issue_revocation_list(
        self,
        *,
        now: Optional[datetime] = None,
        valid_for: timedelta = timedelta(hours=12),
    ) -> SignedRevocationList:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        sequence = self._next_sequence()
        state = self._state()
        signer, chain = self._status
        doc = sign_revocation_list(
            RevocationListPayload(
                sequence=sequence,
                issued_at=current.isoformat(),
                next_update=(current + valid_for).isoformat(),
                revoked_key_ids=tuple(sorted(set(state["revoked_key_ids"]))),
                revoked_serials=tuple(sorted(set(state["revoked_serials"]))),
            ),
            signer,
            chain,
        )
        self.feed.publish_revocation_list(doc)
        return doc

    def issue_kill_switch(
        self,
        *,
        disable_auto_promotion: bool,
        rollback_patch_ids: Iterable[str] = (),
        reason: KillReason,
        now: Optional[datetime] = None,
    ) -> SignedKillSwitch:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        signer, chain = self._status
        doc = sign_kill_switch(
            KillSwitchPayload(
                sequence=self._next_sequence(),
                issued_at=current.isoformat(),
                disable_auto_promotion=disable_auto_promotion,
                rollback_patch_ids=tuple(sorted(set(rollback_patch_ids))),
                reason=reason,
            ),
            signer,
            chain,
        )
        self.feed.publish_kill_switch(doc)
        return doc

    def revoke_contributor(
        self, subject: str, *, now: Optional[datetime] = None
    ) -> tuple[SignedRevocationList, Optional[SignedKillSwitch]]:
        """Revoke a contributor's certificates and roll back its patches.

        Uses the internal audit log: the contributor's key ids and serials go
        on the next revocation list, and every patch it contributed goes on a
        kill switch rollback entry.
        """
        keys, serials, patches = set(), set(), set()
        for entry in self.audit_log():
            if entry.get("contributor_subject") != subject:
                continue
            if entry["event"] == "contribution_received":
                keys.add(entry["contributor_signer_key_id"])
                serials.add(entry["contributor_serial"])
            elif entry["event"] == "patch_distributed":
                patches.add(entry["patch_id"])
        with self._state_lock():
            state = self._state()
            state["revoked_key_ids"] = sorted(set(state["revoked_key_ids"]) | keys)
            state["revoked_serials"] = sorted(set(state["revoked_serials"]) | serials)
            state["revoked_subjects"] = sorted(
                set(state["revoked_subjects"]) | {subject}
            )
            self._save_state(state)
        self._audit(
            {
                "event": "contributor_revoked",
                "contributor_subject": subject,
                "patch_ids": sorted(patches),
                "at": (now or datetime.now(timezone.utc)).isoformat(),
            }
        )
        kill = None
        if patches:
            kill = self.issue_kill_switch(
                disable_auto_promotion=False,
                rollback_patch_ids=patches,
                reason="contributor_revoked",
                now=now,
            )
        return self.issue_revocation_list(now=now), kill


__all__ = [
    "FingerprintRegistrationPayload",
    "FleetHub",
    "HubDecision",
    "HubRefused",
    "MIN_INDEPENDENT_TENANTS",
    "SignedFingerprintRegistration",
    "register_fingerprint",
]
