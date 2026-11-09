"""Export a structural patch contribution from an approved local repair.

Export is off until an admin allows the app family or its domain in this
tenant's opt-in file (:mod:`openadapt_flow.fleet.optin`). Only a repair that
a person approved on this tenant can be exported. The
exporter reads the two bundles the approval bound (by content digest), keeps
the structural anchor change of each step, flags identity-evidence changes by
field name, and refuses anything it cannot express without tenant data:

* a step whose anchor change is pixel-only (no structural locator on one
  side): template crops are screenshots of this tenant's records;
* a selector or automation id that tests text or looks like a record key;
* a repair that adds steps (a taught branch is a program change, not a
  remap);
* a repair that weakens the safety contract, or one that is itself an
  imported fleet patch (it is not this tenant's to re-sign).

After building the payload it runs :func:`~openadapt_flow.fleet.patch.find_leaks`
against every data value in both bundles and their digests, and refuses on
any hit. Then it signs a :class:`~openadapt_flow.fleet.patch.PatchContribution`
with this tenant's ``contributor`` certificate. The contribution goes to the
fleet hub (:mod:`openadapt_flow.fleet.hub`), which re-signs it for
distribution; receivers never see this tenant's certificate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from pydantic import BaseModel

from openadapt_flow.fleet.fingerprint import AppVersionFingerprint
from openadapt_flow.fleet.optin import FleetOptIn, FleetOptInError
from openadapt_flow.fleet.patch import (
    AnchorMap,
    ContributionPayload,
    PatchContribution,
    PatchRedactionError,
    StructuralAnchorRef,
    StructuralPatchPayload,
    find_leaks,
    sign_contribution,
)
from openadapt_flow.fleet.signing import (
    CertificateChain,
    KeyCustody,
    PatchSignatureError,
)
from openadapt_flow.ir import Anchor, StructuralLocator, Workflow
from openadapt_flow.repair.candidate import RepairCandidate, value_digest
from openadapt_flow.repair.lifecycle import RepairLifecycleError, RepairStore
from openadapt_flow.runtime.healing.patch import IDENTITY_FIELDS

#: Lifecycle states in which a repair carries a person's approval.
EXPORTABLE_STATES = ("approved", "staged", "canary", "active")


class PatchExportError(ValueError):
    """The repair cannot be exported as a structural patch."""


@dataclass
class ExportResult:
    contribution: PatchContribution
    #: Steps whose change was visual only and stayed local.
    visual_only_steps: int = 0
    notes: list[str] = field(default_factory=list)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def bundle_data_values(workflow: Workflow) -> tuple[list[str], list[str]]:
    """Every data value a bundle holds, and every digest of one.

    Data values are what a person typed or what the app showed about a
    record: parameters and their examples, typed text, identity bands, window
    titles, effect and API binding contents. The digests are the
    identity-template hashes and salt and the value digests a repair candidate
    records.

    Control labels are not included: ``ocr_text``, landmarks, intents, and a
    control's accessible name (``Search``) routinely share words with its
    selector (``#member-search``). The patch has no field that holds a label
    or a name, and a record's name that leaked into a selector is caught
    through the identity band, which carries it.
    """
    values: list[str] = []
    digests: list[str] = []
    values.extend(str(v) for v in workflow.params.values())
    for spec in workflow.param_specs.values():
        if spec.example is not None and not isinstance(spec.example, bool):
            values.append(str(spec.example))
        values.extend(spec.choices)
    for step in workflow.steps:
        if step.text:
            values.append(step.text)
        for anchor in (step.anchor, step.drag_end_anchor):
            if anchor is None:
                continue
            for text in (anchor.context_text, anchor.structured_identity):
                if text:
                    values.append(text)
            if anchor.structural is not None and anchor.structural.window_name:
                values.append(anchor.structural.window_name)
            if anchor.identity_template is not None:
                digests.extend(
                    s for s in _strings(anchor.identity_template) if len(s) >= 16
                )
            for name in IDENTITY_FIELDS:
                current = getattr(anchor, name)
                if current is not None:
                    dumped = (
                        current.model_dump(mode="json")
                        if isinstance(current, BaseModel)
                        else current
                    )
                    digest = value_digest(dumped)
                    if digest:
                        digests.append(digest)
        for effect in step.effects:
            values.extend(_strings(effect))
        if step.api_binding is not None:
            values.extend(_strings(step.api_binding))
    return values, digests


def _ref(locator: StructuralLocator) -> StructuralAnchorRef:
    return StructuralAnchorRef(
        selector=locator.selector,
        frame_path=tuple(locator.frame_path) if locator.frame_path else None,
        role=locator.role,
        automation_id=locator.automation_id,
    )


def _identity_changes(old: Anchor, new: Anchor) -> tuple[str, ...]:
    return tuple(
        sorted(
            name for name in IDENTITY_FIELDS if getattr(old, name) != getattr(new, name)
        )
    )


def _load_bound(
    path: Optional[str], store: RepairStore, digest: str, label: str
) -> Workflow:
    staged = store.bundle_path(digest)
    source = staged if staged.is_dir() else (Path(path) if path else None)
    if source is None or not source.is_dir():
        raise PatchExportError(f"the approved {label} bundle is not on disk")
    workflow = Workflow.load(source)
    actual = workflow.manifest.content_digest if workflow.manifest else ""
    if actual != digest:
        raise PatchExportError(
            f"the {label} bundle changed since it was approved; refusing to export"
        )
    return workflow


def _require_exportable(candidate: RepairCandidate) -> None:
    if candidate.state not in EXPORTABLE_STATES:
        raise PatchExportError(
            f"candidate {candidate.candidate_id} is {candidate.state!r}; only an "
            "approved repair can be exported"
        )
    approval = candidate.approval
    if approval is None or (
        approval.prior_content_digest != candidate.prior_content_digest
        or approval.proposed_content_digest != candidate.proposed_content_digest
    ):
        raise PatchExportError("the approval does not bind this repair's exact bundles")
    if approval.pre_approval_policy_sha256 is not None:
        raise PatchExportError(
            "this repair was approved by policy, not by a person on this tenant"
        )
    if candidate.source == "fleet_patch" or candidate.fleet_patch is not None:
        raise PatchExportError(
            "an imported fleet patch is not this tenant's to re-sign; the "
            "original patch is the one to share"
        )
    if candidate.contract_weakenings:
        raise PatchExportError("a repair that weakens the contract is never shared")


def build_anchor_maps(
    prior: Workflow, proposed: Workflow
) -> tuple[list[AnchorMap], int]:
    prior_steps = {step.id: step for step in prior.steps}
    added = [step.id for step in proposed.steps if step.id not in prior_steps]
    if added:
        raise PatchExportError(
            "the repair adds steps; a structural patch only remaps anchors"
        )
    maps: list[AnchorMap] = []
    visual_only = 0
    for step in proposed.steps:
        old_anchor = prior_steps[step.id].anchor
        new_anchor = step.anchor
        if old_anchor is None and new_anchor is None:
            continue
        if old_anchor is None or new_anchor is None:
            raise PatchExportError("the repair adds or removes a step's anchor")
        old_loc, new_loc = old_anchor.structural, new_anchor.structural
        identity = _identity_changes(old_anchor, new_anchor)
        if old_loc == new_loc and not identity:
            if old_anchor != new_anchor:
                visual_only += 1
            continue
        if old_loc is None or new_loc is None:
            raise PatchExportError(
                "a changed step has no structural anchor on one side; a "
                "pixel-only repair is screenshots of this tenant's screens and "
                "is never shared"
            )
        try:
            old_ref, new_ref = _ref(old_loc), _ref(new_loc)
        except (PatchRedactionError, ValueError) as exc:
            raise PatchExportError(f"anchor cannot be shared: {exc}") from exc
        maps.append(
            AnchorMap(
                old=old_ref,
                new=new_ref,
                name_unchanged=old_loc.name == new_loc.name,
                identity_fields_changed=identity,
            )
        )
    if not maps:
        raise PatchExportError("the repair changes no structural anchor")
    return maps, visual_only


def export_patch(
    store: RepairStore,
    candidate_id: str,
    *,
    workflow_family: str,
    app_before: AppVersionFingerprint,
    app_after: AppVersionFingerprint,
    signer: KeyCustody,
    chain: CertificateChain,
    opt_in: FleetOptIn,
    origin_host: Optional[str] = None,
    now: Optional[datetime] = None,
) -> ExportResult:
    """Build, leak-check, and sign a contribution from an approved local repair.

    ``origin_host`` is the host of the application as this tenant recorded
    it, used only to match a domain entry in ``opt_in``. It is not exported.
    """
    try:
        opt_in.require("export", app_after.app_family, origin_host)
    except FleetOptInError as exc:
        raise PatchExportError(str(exc)) from exc
    if chain.signer.payload.purpose != "contributor":
        raise PatchExportError("export needs this tenant's contributor certificate")
    if app_after.app_family not in chain.signer.payload.app_families:
        raise PatchExportError(
            "the contributor certificate does not cover this app family"
        )
    try:
        candidate = store.load_candidate(candidate_id)
    except RepairLifecycleError as exc:
        raise PatchExportError(str(exc)) from exc
    _require_exportable(candidate)
    prior = _load_bound(
        candidate.prior_bundle_path, store, candidate.prior_content_digest, "prior"
    )
    proposed = _load_bound(
        candidate.proposed_bundle_path,
        store,
        candidate.proposed_content_digest,
        "proposed",
    )
    maps, visual_only = build_anchor_maps(prior, proposed)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload = StructuralPatchPayload(
        workflow_family=workflow_family,
        app_before=app_before,
        app_after=app_after,
        maps=tuple(maps),
        created_at=current.isoformat(),
    )
    values: list[str] = []
    digests: list[str] = [
        candidate.prior_content_digest,
        candidate.proposed_content_digest,
    ]
    for workflow in (prior, proposed):
        found_values, found_digests = bundle_data_values(workflow)
        values.extend(found_values)
        digests.extend(found_digests)
    for change in candidate.binding_changes:
        digests.extend(d for d in (change.old_sha256, change.new_sha256) if d)
    for fingerprint in candidate.failure_fingerprints:
        digests.append(fingerprint.fingerprint)
    leaks = find_leaks("\n".join(payload.free_strings()), values)
    leaks += find_leaks(payload.model_dump_json(), (), digests=digests)
    if leaks:
        raise PatchExportError(
            "the patch would carry tenant data (" + "; ".join(leaks) + "); refusing"
        )
    try:
        contribution = sign_contribution(
            ContributionPayload(
                patch=payload,
                source_prior_bundle_sha256=candidate.prior_content_digest,
                source_bundle_sha256=candidate.proposed_content_digest,
                contributed_at=current.isoformat(),
            ),
            signer,
            chain,
        )
    except PatchSignatureError as exc:
        raise PatchExportError(str(exc)) from exc
    notes = []
    if payload.identity_evidence_changed:
        notes.append(
            "this patch flags identity evidence; every receiving tenant needs a "
            "person to review it"
        )
    return ExportResult(
        contribution=contribution, visual_only_steps=visual_only, notes=notes
    )


__all__ = [
    "EXPORTABLE_STATES",
    "ExportResult",
    "PatchExportError",
    "build_anchor_maps",
    "bundle_data_values",
    "export_patch",
]
