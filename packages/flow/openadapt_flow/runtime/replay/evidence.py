# mypy: disable-error-code="empty-body"
"""Bundle assets, anchor healing and step evidence keys.

Moved verbatim from ``openadapt_flow.runtime.replayer.Replayer`` by
scripts/extract_mixin.py (docs/design/DECOMPOSITION.md, section 4.1).
``_AssetsHealingAndEvidenceHost`` declares, for mypy only, what this code reads from the
Replayer it is mixed into.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any  # noqa: F401

from openadapt_flow import sealed_set as _sealed
from openadapt_flow.runtime import heal as heal_mod
from openadapt_flow.runtime import healing as healing_mod

if TYPE_CHECKING:
    from typing import Any, Optional

    from openadapt_flow.ir import Region, Resolution, Step, Workflow
    from openadapt_flow.runtime.authorization import GovernedRunAuthorization


class _AssetsHealingAndEvidenceHost:
    def _accept_healed_anchor_in_workflow_snapshot(
        self, workflow: Workflow, event: Any
    ) -> None: ...

    _execution_workflow_source: Optional[Workflow]
    _governed_asset_hashes: dict[str, str]
    _governed_asset_mutation: Optional[str]
    _governed_asset_snapshot: dict[str, bytes]
    _governed_plaintext_assets: Any
    governed_authorization: Optional[GovernedRunAuthorization]
    vision: Any


if TYPE_CHECKING:
    _Base = _AssetsHealingAndEvidenceHost
else:
    _Base = object


class AssetsHealingAndEvidence(_Base):
    """Bundle assets, anchor healing and step evidence keys."""

    def _asset_bytes(
        self,
        bundle_dir: Path,
        rel: str,
        *,
        workflow: Optional[Workflow] = None,
    ) -> Optional[bytes]:
        """Return the verified run snapshot for governed asset consumers."""
        if not rel:
            # No crop: a record-selecting click rebound to another record
            # (runtime.target_param) carries none.
            return None
        if self.governed_authorization is not None:
            data = self._governed_asset_snapshot.get(rel)
            expected = self._governed_asset_hashes.get(rel)
            if self._governed_plaintext_assets and expected is not None:
                path = Path(bundle_dir) / rel
                try:
                    current = path.read_bytes()
                except OSError:
                    current = b""
                if hashlib.sha256(current).hexdigest() != expected:
                    self._governed_asset_mutation = (
                        f"governed sealed asset {rel!r} changed after admission; "
                        "refusing to act; run aborted"
                    )
            return data
        if workflow is not None and workflow.encrypted:
            decrypted = workflow.decrypted_template(rel)
            if decrypted is not None:
                return decrypted
        path = Path(bundle_dir) / rel
        return path.read_bytes() if path.is_file() else None

    def _heal_step(
        self,
        step: Step,
        resolution: Resolution,
        matched_region: Region,
        frame_png: bytes,
        workflow: Workflow,
        run_dir: Path,
        new_crops: dict[str, bytes],
    ):
        """Build, govern, and persist a repair candidate.

        A heal is a governed PATCH, not a silent bundle swap: the raw event is
        wrapped in a reviewable :class:`~openadapt_flow.runtime.healing.HealPatch`
        and run through the regression gate (identity + effect + risk) before
        it may touch the workflow. A patch that would weaken the step's
        identity band -- the reviewed context-drop bug -- is QUARANTINED
        (persisted under ``run_dir/heals/<step_id>/patch.json`` for review)
        and NOT applied; the caller halts the run. A gate-passing local repair
        can update the in-memory workflow. A gate-passing repair discovered
        under exact authorization remains a promotable candidate, because the
        running program cannot change after admission.
        """
        event, crop_png = heal_mod.build_heal_event(
            step, resolution, matched_region, frame_png, self.vision
        )
        outcome = healing_mod.govern_heal(step, event, run_dir=run_dir)
        if outcome.promoted:
            if self.governed_authorization is None:
                heal_mod.apply_heal(workflow, event)
                source = self._execution_workflow_source
                if source is not None and source is not workflow:
                    heal_mod.apply_heal(source, event)
                self._accept_healed_anchor_in_workflow_snapshot(workflow, event)
                new_crops[step.id] = crop_png
            else:
                # Exact authorization binds the admitted workflow semantics.
                # Keep a gate-passing repair as a reviewable candidate for a
                # later qualified bundle. Never change the program that is
                # currently executing under that authorization.
                outcome.patch.status = "promotable"
                healing_mod.persist_patch(outcome.patch, run_dir)
            heal_mod.persist_heal(event, crop_png, frame_png, run_dir)
        return outcome

    @staticmethod
    def _new_step_evidence_key(run_dir: Path, step_id: str) -> str:
        """Allocate one stable, collision-free file key for an action occurrence."""

        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", step_id).strip("._-") or "step"
        if safe != step_id:
            safe = f"{safe}_{hashlib.sha256(step_id.encode()).hexdigest()[:8]}"
        steps_dir = Path(run_dir) / "steps"
        candidate = safe
        occurrence = 1
        while any(
            _sealed.exists(steps_dir / f"{candidate}_{suffix}.png")
            for suffix in ("before", "after", "attended-before", "attended-after")
        ):
            occurrence += 1
            candidate = f"{safe}__{occurrence:04d}"
        return candidate
