"""Import a signed structural patch as a repair candidate on this tenant.

Import never activates anything. It:

1. verifies the signature chain against this tenant's pinned fleet roots;
2. checks that this tenant runs the application version the patch was made
   for (``app_after``), by structural fingerprint;
3. applies the anchor maps to a COPY of this tenant's own bundle. Only the
   structural locator changes. This tenant's identity evidence, templates,
   effects, and parameters are left exactly as they were;
4. registers the copy as a ``fleet_patch`` candidate in the existing repair
   lifecycle (:class:`~openadapt_flow.repair.lifecycle.RepairStore`), with the
   contract invariants checked as for any other repair.

What happens next is decided by :mod:`openadapt_flow.fleet.policy`.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from openadapt_flow.fleet.fingerprint import AppVersionFingerprint
from openadapt_flow.fleet.patch import AnchorMap, SignedStructuralPatch, verify_patch
from openadapt_flow.fleet.signing import FleetTrust, key_id
from openadapt_flow.ir import (
    StructuralLocator,
    Workflow,
    structural_locator_is_locatable,
)
from openadapt_flow.repair.candidate import FleetPatchProvenance, RepairCandidate
from openadapt_flow.repair.lifecycle import RepairStore
from openadapt_flow.repair.registration import (
    DETACHED_CANDIDATE_RELPATH,
    build_candidate,
)


class PatchImportError(ValueError):
    """The patch does not apply to this tenant. Nothing was registered."""


@dataclass
class ImportResult:
    candidate: RepairCandidate
    proposed_bundle: Path
    patched_steps: list[str]
    identity_steps: list[str]


def _apply(locator: StructuralLocator, item: AnchorMap) -> StructuralLocator:
    new = item.new
    updated = StructuralLocator(
        selector=new.selector,
        frame_path=list(new.frame_path) if new.frame_path else None,
        role=new.role,
        name=locator.name if item.name_unchanged else None,
        automation_id=new.automation_id,
        window_name=locator.window_name,
    )
    if not structural_locator_is_locatable(updated):
        raise PatchImportError("a remapped anchor would not be locatable")
    return updated


def import_patch(
    store: RepairStore,
    patch: SignedStructuralPatch,
    *,
    trust: FleetTrust,
    tenant_bundle: Path | str,
    tenant_app: AppVersionFingerprint,
    out_dir: Path | str,
    now: Optional[datetime] = None,
) -> ImportResult:
    """Verify, apply to a copy of ``tenant_bundle``, and register a candidate.

    Raises :class:`~openadapt_flow.fleet.signing.PatchSignatureError` on any
    signature-chain failure and :class:`PatchImportError` when the patch does
    not apply. Neither leaves anything in the store or in ``out_dir``.
    """
    verify_patch(patch, trust, now=now)
    payload = patch.payload
    reason = payload.app_after.mismatch(tenant_app)
    if reason is not None:
        raise PatchImportError(
            f"this tenant does not run the app version the patch is for: {reason}"
        )

    source = Path(tenant_bundle)
    workflow = Workflow.load(source)
    if workflow.manifest is None or not workflow.manifest.content_digest:
        raise PatchImportError("this tenant's bundle carries no content digest")

    patched: list[str] = []
    identity_steps: list[str] = []
    replacements: dict[str, StructuralLocator] = {}
    for step in workflow.steps:
        anchor = step.anchor
        if anchor is None or anchor.structural is None:
            continue
        hits = [item for item in payload.maps if item.old.matches(anchor.structural)]
        if not hits:
            continue
        item = hits[0]
        replacements[step.id] = _apply(anchor.structural, item)
        patched.append(step.id)
        if item.touches_identity:
            identity_steps.append(step.id)
    if not patched:
        raise PatchImportError("no step of this bundle uses an anchor the patch remaps")

    destination = Path(out_dir)
    if destination.exists():
        raise PatchImportError(f"{destination} already exists")
    shutil.copytree(source, destination)
    try:
        stale = destination / DETACHED_CANDIDATE_RELPATH
        if stale.exists():
            stale.unlink()
        proposed = Workflow.load(destination)
        for step in proposed.steps:
            if step.id in replacements and step.anchor is not None:
                step.anchor = step.anchor.model_copy(
                    update={"structural": replacements[step.id]}
                )
        proposed.save(destination)

        candidate = build_candidate(source, destination, source="fleet_patch")
        verified_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        candidate.fleet_patch = FleetPatchProvenance(
            patch_id=patch.patch_id,
            app_family=payload.app_after.app_family,
            source_bundle_sha256=payload.source_bundle_sha256,
            source_prior_bundle_sha256=payload.source_prior_bundle_sha256,
            app_fingerprint_sha256=payload.app_after.fingerprint_sha256(),
            signer_key_id=patch.certificate.signer_key_id,
            root_key_id=key_id(trust.roots[patch.certificate.payload.root_key_id]),
            source_tenant_pseudonym=payload.source_tenant_pseudonym,
            identity_evidence_changed=bool(identity_steps),
            patched_steps=patched,
            signature_chain_verified_at=verified_at.isoformat(),
        )
        detached = destination / DETACHED_CANDIDATE_RELPATH
        detached.parent.mkdir(parents=True, exist_ok=True)
        detached.write_text(candidate.model_dump_json(indent=2))
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    registered = store.add_candidate(candidate)
    return ImportResult(
        candidate=registered,
        proposed_bundle=destination,
        patched_steps=patched,
        identity_steps=identity_steps,
    )


__all__ = ["ImportResult", "PatchImportError", "import_patch"]
