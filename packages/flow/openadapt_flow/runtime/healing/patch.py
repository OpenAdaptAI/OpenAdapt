"""Reviewable heal PATCH: what a repair changed, as an auditable diff.

A heal must never be a silently-swapped bundle. :class:`HealPatch` turns a
:class:`~openadapt_flow.ir.HealEvent` into a structured, diffable record of
exactly what a repair changed (which anchor field, from what to what) and how
it moved the step's IDENTITY band (the evidence the pre-click identity gate
keys on). It is the unit the governance gate rules on and the artifact an
operator reviews before a patch is promoted into a healed bundle.

The invariant a patch exists to enforce: a repair may change HOW an operation
is performed (its locator/rung), but never silently weaken WHAT it means or
how its effects are verified. The patch makes any such weakening VISIBLE
(``identity_before`` vs ``identity_after``) so the gate can refuse it rather
than let it pass green.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from openadapt_flow.ir import Anchor, HealEvent, Rung

# Anchor fields that carry IDENTITY evidence -- the "WHAT it means" of a step.
# A heal may freely change locator fields (region/click_point/ocr_text); it
# may NEVER silently drop any of these relative to the pre-heal anchor.
IDENTITY_FIELDS: tuple[str, ...] = (
    "context_text",
    "structured_identity",
    "identity_template",
    "identifier_crop",
    "identifier_region",
)

# Anchor fields that describe HOW a target is located -- safe for a heal to
# change; these are the mutable evidence the resolution ladder heals through.
LOCATOR_FIELDS: tuple[str, ...] = (
    "template",
    "region",
    "click_point",
    "ocr_text",
    "landmarks",
    "search_pad",
)

PatchStatus = Literal[
    "candidate",  # built, not yet gated
    "promotable",  # passed the regression gate; safe to apply/promote
    "quarantined",  # failed the gate; must NOT be applied -- run halts
    "promoted",  # applied to the workflow and folded into a healed bundle
    "rolled_back",  # applied then reverted (canary regression)
]


class AnchorChange(BaseModel):
    """One field that a heal changed on the step's anchor (old -> new)."""

    field: str
    identity: bool = Field(description="True when this field carries identity evidence")
    old: Any = None
    new: Any = None


class IdentitySnapshot(BaseModel):
    """The identity evidence an anchor carries, at one point in time.

    ``armed`` mirrors the replayer's own coverage rule
    (:meth:`Replayer._record_identity_coverage`): a step is identity-armed
    when it carries a recorded context band OR structured identity text. The
    pixel/VLM identifier crop is additional evidence but does not, on its own,
    arm the OCR/structured gate -- so it is tracked but not counted as arming,
    exactly as the replayer counts it.
    """

    context_text: Optional[str] = None
    structured_identity: Optional[str] = None
    identifier_crop: Optional[str] = None
    identifier_region: Optional[Any] = None
    # True when the anchor carried a PHI-free salted-hash identity template
    # (audit REM-2) rather than plaintext identity fields. Recorded for the
    # audit trail; a template present arms the gate exactly as plaintext does.
    has_identity_template: bool = False
    armed: bool = False

    @classmethod
    def from_anchor(cls, anchor: Anchor) -> "IdentitySnapshot":
        return cls(
            context_text=anchor.context_text,
            structured_identity=anchor.structured_identity,
            identifier_crop=anchor.identifier_crop,
            identifier_region=anchor.identifier_region,
            has_identity_template=anchor.identity_template is not None,
            armed=bool(
                anchor.context_text
                or anchor.structured_identity
                or anchor.identity_template
            ),
        )


class HealPatch(BaseModel):
    """A reviewable, diffable description of a single heal.

    Built from a :class:`~openadapt_flow.ir.HealEvent` before the event is
    applied. Carries the field-level changes and the identity-band transition
    so the governance gate (:mod:`openadapt_flow.runtime.healing.governance`)
    can decide PROMOTABLE vs QUARANTINED, and so an operator can audit the
    repair as a diff rather than a swapped binary bundle.
    """

    step_id: str
    rung_used: Rung
    changes: list[AnchorChange] = Field(default_factory=list)
    identity_before: IdentitySnapshot
    identity_after: IdentitySnapshot
    status: PatchStatus = "candidate"
    reject_reason: Optional[str] = None

    @classmethod
    def from_event(cls, event: HealEvent) -> "HealPatch":
        """Build a candidate patch from a (not-yet-applied) heal event."""
        old, new = event.old_anchor, event.new_anchor
        changes: list[AnchorChange] = []
        for field in IDENTITY_FIELDS + LOCATOR_FIELDS:
            old_val = getattr(old, field)
            new_val = getattr(new, field)
            if old_val != new_val:
                changes.append(
                    AnchorChange(
                        field=field,
                        identity=field in IDENTITY_FIELDS,
                        old=old_val,
                        new=new_val,
                    )
                )
        return cls(
            step_id=event.step_id,
            rung_used=event.rung_used,
            changes=changes,
            identity_before=IdentitySnapshot.from_anchor(old),
            identity_after=IdentitySnapshot.from_anchor(new),
        )

    def identity_changes(self) -> list[AnchorChange]:
        """The subset of changes that touch identity evidence."""
        return [c for c in self.changes if c.identity]

    def diff(self) -> str:
        """A human-readable, reviewable diff of the repair.

        Identity-field changes are called out separately from locator
        changes so a reviewer sees at a glance whether a repair touched WHAT
        the step means (identity) or only HOW it is located (locator).
        """
        lines = [
            f"HealPatch step={self.step_id} rung={self.rung_used} status={self.status}",
            f"  identity armed: {self.identity_before.armed} -> "
            f"{self.identity_after.armed}",
        ]
        if self.reject_reason:
            lines.append(f"  REJECTED: {self.reject_reason}")
        id_changes = self.identity_changes()
        loc_changes = [c for c in self.changes if not c.identity]
        if id_changes:
            lines.append("  identity changes:")
            for c in id_changes:
                lines.append(f"    - {c.field}: {c.old!r} -> {c.new!r}")
        if loc_changes:
            lines.append("  locator changes:")
            for c in loc_changes:
                lines.append(f"    - {c.field}: {c.old!r} -> {c.new!r}")
        return "\n".join(lines)
