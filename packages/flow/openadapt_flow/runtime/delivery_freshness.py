"""Last-boundary checks for already-issued managed input permissions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openadapt_flow.runtime.durable.authority import (
    DurableAuthority,
    DurableAuthorityBusy,
)


class InitialDeliveryGuard:
    """One unacknowledged permission at a time for an initial managed run."""

    def __init__(self, run_dir: Path, durable_run: Any) -> None:
        self.authority = DurableAuthority(run_dir, durable_run.store)
        self._manifest = durable_run._manifest
        self.pending_remote_permit: Any = None

    def before_delivery(self) -> None:
        if self.pending_remote_permit is not None:
            raise DurableAuthorityBusy(
                "a prior production delivery lacks an acknowledgment receipt"
            )
        self.pending_remote_permit = self.authority.before_initial_delivery(
            self._manifest
        )

    def require_delivery_fresh(self) -> None:
        if self.pending_remote_permit is None:
            raise DurableAuthorityBusy(
                "the managed input has no issued delivery permit"
            )
        self.authority.require_initial_delivery_fresh(
            self._manifest, self.pending_remote_permit
        )

    def acknowledge_delivery(self) -> None:
        pending = self.pending_remote_permit
        if pending is None:
            return
        self.authority.acknowledge_remote_delivery(self._manifest, pending)
        self.pending_remote_permit = None


def recheck_pending_delivery(
    replayer: Any, guards: tuple[Any, ...], result: Any
) -> None:
    """Check again after preparation and immediately before the input call.

    A refusal here proves this input was never attempted. Do not turn an
    expired permission into post-input uncertainty or fetch another permit.
    """
    from openadapt_flow.backend import StructuralResolutionRefused

    try:
        if replayer.managed_dispatch_binding is not None:
            workflow = replayer._execution_workflow_source
            if workflow is None:
                raise DurableAuthorityBusy("the managed execution snapshot is missing")
            refusal = (
                replayer._managed_dispatch_refusal()
                or replayer._workflow_snapshot_refusal(workflow)
                or replayer._governed_authorization_snapshot_refusal()
                or replayer._production_qualification_refusal(workflow)
            )
            if refusal is not None:
                raise DurableAuthorityBusy(refusal)
        # Check the receiver clock last, after any local trust/snapshot I/O.
        for guard in guards:
            check = getattr(guard, "require_delivery_fresh", None)
            if check is not None:
                check()
    except Exception as exc:  # noqa: BLE001 - an unreadable permission grants nothing
        result.safety_halt = True
        result.failure_category = "continuation_preempted"
        raise StructuralResolutionRefused(
            "managed delivery authority is no longer fresh before input"
        ) from exc
