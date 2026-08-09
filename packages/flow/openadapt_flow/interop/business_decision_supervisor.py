"""Route typed Cloud answers to exact customer-controlled paused runs.

This supervisor publishes every qualified active task and records one leased
answer in the exact local durable journal. It does not resume a workflow or
actuate an application. The normal runtime consumes the answer later and owns
fresh state checks, identity checks, actuation, and effect verification.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol

from openadapt_flow.console import data
from openadapt_flow.console.decision_relay import RelayTransport
from openadapt_flow.crypto import CryptoError
from openadapt_flow.interop.business_decision_cloud import (
    BusinessDecisionCloudDelivery,
    BusinessDecisionCloudRefused,
    BusinessDecisionCloudRelay,
    poll_business_decision_cloud_answer,
)
from openadapt_flow.runtime.durable.business_decision import (
    BusinessDecisionRefused,
    BusinessDecisionStore,
)
from openadapt_flow.runtime.durable.checkpoint import CheckpointStore


class BusinessDecisionRelayFactory(Protocol):
    """Build one exact qualified relay from a local durable run."""

    def __call__(
        self,
        run_dir: Path,
        store: BusinessDecisionStore,
        transport: RelayTransport,
        at: str,
    ) -> BusinessDecisionCloudRelay: ...


class BusinessDecisionCheckpointKeyResolver(Protocol):
    """Return the durable checkpoint key for one exact local run."""

    def __call__(self, run_dir: Path) -> Optional[str]: ...


class UnmatchedBusinessDecisionRefuser(Protocol):
    """Close a leased answer that cannot bind to a current local task."""

    def __call__(self, delivery: BusinessDecisionCloudDelivery, at: str) -> bool: ...


@dataclass(frozen=True)
class BoundBusinessDecisionRelay:
    """One active local pause and its exact portable task."""

    run_dir: Path
    relay: BusinessDecisionCloudRelay
    answer_recorded: bool


@dataclass(frozen=True)
class BusinessDecisionPublishReport:
    """PHI-free counts from one publication pass."""

    published: int = 0
    already_published: int = 0
    uncertain: int = 0
    not_projectable: int = 0
    refused: int = 0


@dataclass(frozen=True)
class BusinessDecisionSupervisorReport:
    """One poll and record cycle."""

    publishes: BusinessDecisionPublishReport
    answer_received: bool = False
    answer_matched: bool = False
    answer_recorded: bool = False
    receipt_confirmed: bool = False
    unmatched_refusal_confirmed: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BusinessDecisionSupervisor:
    """Own the typed-decision transport for all paused runs under one root."""

    def __init__(
        self,
        runs_root: Path | str,
        *,
        transport: RelayTransport,
        relay_factory: BusinessDecisionRelayFactory,
        checkpoint_key_resolver: BusinessDecisionCheckpointKeyResolver,
        unmatched_refuser: UnmatchedBusinessDecisionRefuser,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._runs_root = Path(runs_root)
        self._transport = transport
        self._relay_factory = relay_factory
        self._checkpoint_key_resolver = checkpoint_key_resolver
        self._unmatched_refuser = unmatched_refuser
        self._now = now or _now

    def active_relays(self, *, at: str) -> tuple[list[BoundBusinessDecisionRelay], int]:
        """Build exact relays for current business-decision pauses."""

        relays: list[BoundBusinessDecisionRelay] = []
        not_projectable = 0
        for run_dir in data._scan(self._runs_root, data._is_run_dir):
            try:
                checkpoint_key = self._checkpoint_key_resolver(run_dir)
                pending = CheckpointStore(run_dir, key=checkpoint_key).read_pending()
                if pending is None or pending.category != "business_decision":
                    continue
                store = BusinessDecisionStore(run_dir, checkpoint_key=checkpoint_key)
                if not store.active_path.is_file():
                    not_projectable += 1
                    continue
                request, _request_sha256 = store.read_active_request()
                retained = store.read_receipt(request.digest)
                relay = self._relay_factory(run_dir, store, self._transport, at)
            except (
                BusinessDecisionRefused,
                BusinessDecisionCloudRefused,
                CryptoError,
                OSError,
                ValueError,
            ):
                not_projectable += 1
                continue
            relays.append(
                BoundBusinessDecisionRelay(
                    run_dir=run_dir,
                    relay=relay,
                    answer_recorded=retained is not None,
                )
            )
        return relays, not_projectable

    @staticmethod
    def _index(
        relays: list[BoundBusinessDecisionRelay],
    ) -> dict[tuple[str, int, str], BoundBusinessDecisionRelay]:
        indexed: dict[tuple[str, int, str], BoundBusinessDecisionRelay] = {}
        for bound in relays:
            key = bound.relay.task_binding
            if key in indexed:
                raise BusinessDecisionCloudRefused(
                    "one portable business decision task names multiple local runs"
                )
            indexed[key] = bound
        return indexed

    def publish(
        self,
        relays: list[BoundBusinessDecisionRelay],
        *,
        at: str,
        not_projectable: int,
    ) -> BusinessDecisionPublishReport:
        """Publish every unanswered task without silencing its peers."""

        published = already = uncertain = refused = 0
        for bound in relays:
            if bound.answer_recorded:
                continue
            try:
                result = bound.relay.publish(at=at)
            except (BusinessDecisionCloudRefused, ValueError):
                refused += 1
                continue
            if result is True:
                published += 1
            elif result is False:
                already += 1
            else:
                uncertain += 1
        return BusinessDecisionPublishReport(
            published=published,
            already_published=already,
            uncertain=uncertain,
            not_projectable=not_projectable,
            refused=refused,
        )

    def serve_once(self, *, wait_s: float = 25.0) -> BusinessDecisionSupervisorReport:
        """Publish all tasks, poll once, and record one exact answer."""

        at = self._now().astimezone(timezone.utc).isoformat()
        relays, not_projectable = self.active_relays(at=at)
        indexed = self._index(relays)
        publishes = self.publish(relays, at=at, not_projectable=not_projectable)
        delivery: Optional[BusinessDecisionCloudDelivery] = (
            poll_business_decision_cloud_answer(self._transport, wait_s=wait_s)
        )
        if delivery is None:
            return BusinessDecisionSupervisorReport(publishes=publishes)
        key = (
            delivery.answer.task_id,
            delivery.answer.task_revision,
            delivery.answer.task_digest,
        )
        bound = indexed.get(key)
        if bound is None:
            refused_at = self._now().astimezone(timezone.utc).isoformat()
            confirmed = self._unmatched_refuser(delivery, refused_at)
            return BusinessDecisionSupervisorReport(
                publishes=publishes,
                answer_received=True,
                unmatched_refusal_confirmed=confirmed,
            )
        recorded_at = self._now().astimezone(timezone.utc).isoformat()
        cycle = bound.relay.record(delivery, at=recorded_at)
        return BusinessDecisionSupervisorReport(
            publishes=publishes,
            answer_received=True,
            answer_matched=True,
            answer_recorded=True,
            receipt_confirmed=cycle.receipt_confirmed,
        )


__all__ = [
    "BoundBusinessDecisionRelay",
    "BusinessDecisionPublishReport",
    "BusinessDecisionCheckpointKeyResolver",
    "BusinessDecisionRelayFactory",
    "BusinessDecisionSupervisor",
    "BusinessDecisionSupervisorReport",
    "UnmatchedBusinessDecisionRefuser",
]
