"""Reach a phone without opening a port: the runner dials out.

Before this module the two attended-decision delivery paths were:

* the **runner-local portal** — full fidelity, protected screenshot crops, the
  gated control label — reachable from a phone only through an HTTPS ingress
  the customer terminates themselves; and
* the **hosted control plane** — reachable from anywhere, but with nothing on
  the runner that ever spoke to it. The hosted endpoints existed; no client did.

A dental practice will not stand up a reverse proxy, so the first path is not
available to them, and the second was not wired. This module wires it, in the
only shape that needs nothing from the customer's network: the runner makes
**outbound HTTPS requests only** — no inbound port, no port forward, no
certificate, no static address, works behind NAT — following the same
``register -> poll -> lease -> ack`` shape ``openadapt-desktop``'s runner lane
already uses for governed runs.

What crosses, and what does not
-------------------------------

Exactly the :class:`~openadapt_flow.console.human_decisions.RemoteDecisionProjection`
the engine already builds: the signed PHI-free task, and — at
``remote_closed_context`` — the closed-vocabulary
:class:`~openadapt_flow.console.decision_context.RemoteHaltContextV1`. No
screenshot, no OCR text, no observed value, no path, no workflow label, and no
free-text field of any kind. The hosted service is not trusted with protected
content; it is structurally incapable of receiving it.

What a returned decision is, and is not
---------------------------------------

A relayed answer is **presentation and authentication evidence, never execution
authority**. It is admitted through the same
:func:`~openadapt_flow.console.human_decisions.execute_remote_attended_action`
path a directly-connected AAL2 surface uses: the exact pause capability is
re-checked, a single-flight lease is taken, the live application is re-read, and
every contract in ``will_recheck`` is re-proved before anything continues. A tap
on a phone is not a verified business effect and this module cannot make it one.

Uncertainty is reported, never guessed
--------------------------------------

Two boundaries here can fail after a request may already have taken effect, and
both are classified rather than retried:

* :meth:`DecisionRelay.publish` returns
  :attr:`PublishState.UNKNOWN` when a POST left the process without a terminal
  response. The task may or may not be visible on a phone. The caller is told
  ``unknown`` and the local console remains the authoritative surface; nothing
  re-POSTs on the assumption that the first attempt failed.
* :meth:`DecisionRelay.acknowledge` has the same property in the other
  direction. A decision that was executed locally but whose acknowledgement did
  not land stays leased server-side and is re-delivered; the engine's existing
  idempotency key makes the replay a no-op rather than a second execution.

**"Delivered" is not claimed anywhere in this module.** A successful POST proves
the control plane accepted the task, not that a person received or read it.
:class:`PublishOutcome` says ``published``; nothing in this file says
``delivered``, because the runner cannot observe that.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.console.attention import AttentionItem
from openadapt_flow.console.human_decisions import (
    RemoteAttendedActionRequest,
    RemoteDecisionPrincipal,
    RemoteDecisionProjection,
    execute_remote_attended_action,
    portable_remote_decision_task,
)
from openadapt_flow.deployment import DeploymentConfig
from openadapt_flow.interop.decision_relay_transport import (
    HttpxRelayTransport as HttpxRelayTransport,
)
from openadapt_flow.interop.decision_relay_transport import (
    RelayRefused,
    RelayTransport,
    RelayUncertain,
)
from openadapt_flow.interop.decision_relay_transport import (
    resolve_runner_token as resolve_runner_token,
)
from openadapt_flow.runtime.durable.attended import (
    AttendedActionExecutor,
    AttendedActionRefused,
    AttendedDecision,
    AttendedRelayBinding,
)

#: Wire schema of a relayed decision, as minted by the hosted control plane.
RELAY_SCHEMA = "openadapt.human-decision-relay/v2"

TASKS_PATH = "/api/human-decisions/tasks"
POLL_PATH = "/api/human-decisions/relay/poll"


def _ack_path(decision_id: str) -> str:
    return f"/api/human-decisions/relay/{decision_id}/ack"


_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HMAC_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")

#: Every field a relayed decision carries, exactly. A response with any other
#: key is refused rather than filtered: an unexpected field means this client
#: and the control plane disagree about the contract, which is not a condition
#: under which to execute someone's clinical workflow.
_RELAY_KEYS = frozenset(
    {
        "schema_version",
        "decision_id",
        "tenant_id",
        "runner_id",
        "task_id",
        "task_revision",
        "task_digest",
        "task_signature",
        "capability_digest",
        "phase",
        "event_sequence",
        "idempotency_scope_digest",
        "binding_digest",
        "decision_action",
        "action",
        "idempotency_key",
        "actor_id",
        "assurance",
        "submitted_at",
        "expires_at",
        "execution_authority",
        "local_revalidation_required",
        "signature_algorithm",
        "relay_digest",
        "relay_signature",
    }
)

#: The engine action each relayed answer names, mapped to the disposition
#: ``execute_attended_action`` requires for it. The engine refuses a mismatched
#: pair by design, so a single hardcoded disposition silently reduces this lane
#: to Continue only -- every other answer a phone can give would be refused
#: after it was taken. Deriving the disposition from the action is what keeps
#: the relayed vocabulary equal to the local one.
#:
#: ``reject`` TERMINATES the run and ``escalate`` PARKS it; they are separate
#: members for that reason, and a phone that offers Reject must be able to
#: deliver it or the answer distribution loses the disagreement signal the
#: action exists to record.
_ENGINE_DISPOSITION: dict[str, str] = {
    "continue": "completed_by_operator",
    "skip": "not_applicable",
    "reject": "rejected_by_operator",
    "teach": "teach_requested",
    "escalate": "cannot_complete",
    "reconcile": "reconciliation_requested",
}

_ENGINE_ACTIONS = frozenset(_ENGINE_DISPOSITION)

#: The four terminal words the control plane accepts on acknowledgement.
_ACK_RESULTS = frozenset({"accepted", "refused", "stale", "expired"})


class PublishState(str, Enum):
    """What is known about one attempt to make a pause visible remotely."""

    #: The control plane accepted the projection and it is now answerable.
    PUBLISHED = "published"
    #: The control plane already held this exact task revision. Idempotent.
    ALREADY_PUBLISHED = "already_published"
    #: The request may or may not have arrived. Not retried, not claimed.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PublishOutcome:
    """The result of publishing one pause, including the tier it carried."""

    state: PublishState
    #: Which rung of the delivery ladder actually crossed the wire.
    delivery_tier: str
    #: ``True`` when the projection carried the closed halt context. A caller
    #: rendering its own operator copy uses this to avoid promising a remote
    #: surface detail it was not sent.
    carried_context: bool
    task_id: Optional[str] = None

    @property
    def is_certain(self) -> bool:
        return self.state is not PublishState.UNKNOWN


@dataclass(frozen=True)
class RelayedDecision:
    """One verified answer from the hosted surface, not yet executed."""

    decision_id: str
    relay: dict[str, Any]

    @property
    def relay_digest(self) -> str:
        return str(self.relay["relay_digest"])

    @property
    def relay_signature(self) -> str:
        return str(self.relay["relay_signature"])

    @property
    def action(self) -> str:
        return str(self.relay["action"])

    def durable_binding(self) -> AttendedRelayBinding:
        """Exact PHI-free fields required to recover a lost acknowledgement."""
        return AttendedRelayBinding(
            decision_id=self.decision_id,
            relay_digest=self.relay_digest,
            relay_signature=self.relay_signature,
            idempotency_key=str(self.relay["idempotency_key"]),
            capability_digest=str(self.relay["capability_digest"]),
            event_sequence=int(self.relay["event_sequence"]),
            action=self.action,  # type: ignore[arg-type]
        )


def _canonical(payload: Any) -> bytes:
    """The control plane's canonical JSON form, byte for byte.

    ``canonicalJson`` in the hosted service is ``JSON.stringify`` with sorted
    object keys and no whitespace. Every value in a relay is an opaque
    identifier, a digest, an RFC 3339 timestamp, a closed enum, a bounded
    integer, or a boolean — all ASCII — so ``ensure_ascii`` cannot move the
    result, and ``tests/test_decision_relay.py`` pins a fixture produced by the
    hosted signer rather than trusting that argument.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def verify_relay(relay: dict[str, Any], token: str) -> bool:
    """Whether ``relay`` was signed by the holder of ``token``.

    The signature is an HMAC under the runner's own token, so a relay the runner
    cannot verify is one it must not act on: the control plane could not have
    minted it from this runner's registration.
    """
    if not isinstance(relay, dict) or set(relay) != _RELAY_KEYS:
        return False
    digest = relay.get("relay_digest")
    signature = relay.get("relay_signature")
    if not isinstance(digest, str) or not isinstance(signature, str):
        return False
    unsigned = {
        key: value
        for key, value in relay.items()
        if key not in {"relay_digest", "relay_signature"}
    }
    expected_digest = "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()
    signed_body = (
        RELAY_SCHEMA.encode("utf-8")
        + b"\0"
        + _canonical({**unsigned, "relay_digest": expected_digest})
    )
    expected_signature = (
        "hmac-sha256:"
        + hmac.new(token.encode("utf-8"), signed_body, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected_digest, digest) and hmac.compare_digest(
        expected_signature, signature
    )


def _validate_relay_shape(relay: Any) -> dict[str, Any]:
    """Refuse anything that is not exactly one well-formed relay."""
    if not isinstance(relay, dict):
        raise RelayRefused("the relay response was not an object")
    if set(relay) != _RELAY_KEYS:
        raise RelayRefused(
            "the relay response does not match the exact decision contract"
        )
    if relay.get("schema_version") != RELAY_SCHEMA:
        raise RelayRefused("the relay response carries an unsupported schema version")
    if relay.get("assurance") != "aal2":
        raise RelayRefused("the relayed decision was not taken at AAL2")
    if relay.get("execution_authority") != "customer_runtime":
        raise RelayRefused(
            "the relay claims execution authority the customer runtime never cedes"
        )
    if relay.get("local_revalidation_required") is not True:
        raise RelayRefused(
            "the relay does not require local revalidation; refusing to execute it"
        )
    if relay.get("phase") != "paused":
        raise RelayRefused("the relayed decision is not bound to an open pause")
    if relay.get("action") not in _ENGINE_ACTIONS:
        raise RelayRefused(
            "the relayed decision names an action the engine has no map for"
        )
    if not isinstance(relay.get("event_sequence"), int) or isinstance(
        relay.get("event_sequence"), bool
    ):
        raise RelayRefused("the relayed decision has no exact pause event sequence")
    for field, pattern in (
        ("decision_id", _OPAQUE_ID_RE),
        ("tenant_id", _OPAQUE_ID_RE),
        ("runner_id", _OPAQUE_ID_RE),
        ("task_digest", _SHA256_RE),
        ("capability_digest", _SHA256_RE),
        ("idempotency_scope_digest", _SHA256_RE),
        ("binding_digest", _SHA256_RE),
        ("task_signature", _HMAC_RE),
        ("relay_digest", _SHA256_RE),
        ("relay_signature", _HMAC_RE),
    ):
        value = relay.get(field)
        if not isinstance(value, str) or not pattern.match(value):
            raise RelayRefused(f"the relayed decision has an invalid {field}")
    return relay


class DecisionRelay:
    """One runner's outbound lane to the hosted attended-decision surface."""

    def __init__(
        self,
        transport: RelayTransport,
        *,
        token: str,
        deployment: DeploymentConfig,
    ) -> None:
        self._transport = transport
        self._token = token
        self._deployment = deployment
        remote = deployment.human_decisions.remote
        if not remote.enabled or remote.tenant_id is None or remote.runner_id is None:
            raise RelayRefused(
                "the decision relay requires human_decisions.remote.enabled with "
                "an exact tenant and runner binding"
            )
        self._tenant_id = remote.tenant_id
        self._runner_id = remote.runner_id

    # -- outbound ---------------------------------------------------------

    def publish(
        self,
        run_dir: Path,
        item: AttentionItem,
        *,
        timeout_s: float = 15.0,
    ) -> PublishOutcome:
        """Make one open pause answerable from the hosted surface.

        Returns:
            A :class:`PublishOutcome`. ``state`` is ``unknown`` when the request
            may have been delivered; the caller must not re-publish on that
            basis, and must not describe the pause as reachable.
        """
        projection: RemoteDecisionProjection = portable_remote_decision_task(
            run_dir, item, deployment=self._deployment
        )
        payload = projection.model_dump(mode="json")
        try:
            status, body = self._transport.post(
                TASKS_PATH, payload, timeout_s=timeout_s
            )
        except RelayUncertain:
            return PublishOutcome(
                state=PublishState.UNKNOWN,
                delivery_tier=projection.delivery_tier,
                carried_context=projection.halt_context is not None,
            )
        if status >= 500:
            # A server error after the body was accepted is indistinguishable
            # from one before it. Uncertain, not failed.
            return PublishOutcome(
                state=PublishState.UNKNOWN,
                delivery_tier=projection.delivery_tier,
                carried_context=projection.halt_context is not None,
            )
        if status >= 400 or body.get("accepted") is not True:
            raise RelayRefused(
                "the control plane refused the pause projection; the local "
                "console remains the authoritative surface for this decision"
            )
        return PublishOutcome(
            state=(
                PublishState.PUBLISHED
                if body.get("created") is True
                else PublishState.ALREADY_PUBLISHED
            ),
            delivery_tier=projection.delivery_tier,
            carried_context=projection.halt_context is not None,
            task_id=(
                str(body["task_id"]) if isinstance(body.get("task_id"), str) else None
            ),
        )

    # -- inbound ----------------------------------------------------------

    def poll(self, *, wait_s: float = 25.0) -> Optional[RelayedDecision]:
        """Long-poll for one answered decision, or ``None`` if none is waiting.

        Raises:
            RelayRefused: If a decision arrives that this runner cannot verify,
                that is not bound to this runner, or that does not match the
                exact relay contract. A malformed decision is never partially
                honoured.
        """
        try:
            status, body = self._transport.post(
                POLL_PATH, {"wait": wait_s}, timeout_s=wait_s + 10.0
            )
        except RelayUncertain:
            # A poll has no side effect, so an uncertain poll is simply no
            # decision this cycle.
            return None
        if status == 204 or not body:
            return None
        if status >= 400:
            raise RelayRefused("the control plane refused the decision poll")
        relay = _validate_relay_shape(body.get("decision"))
        if not verify_relay(relay, self._token):
            raise RelayRefused(
                "the relayed decision is not signed by this runner's credential"
            )
        if (
            relay["tenant_id"] != self._tenant_id
            or relay["runner_id"] != self._runner_id
        ):
            raise RelayRefused(
                "the relayed decision is scoped to a different tenant or runner"
            )
        return RelayedDecision(decision_id=str(relay["decision_id"]), relay=relay)

    def execute(
        self,
        run_dir: Path,
        item: AttentionItem,
        decision: RelayedDecision,
        *,
        executor: Optional[AttendedActionExecutor] = None,
        key: Optional[str] = None,
    ) -> AttendedDecision:
        """Admit and run one relayed decision through the normal governed path.

        The relay supplies identity and intent only. Everything that makes the
        continuation safe — the exact pause capability, the single-flight lease,
        the fresh live re-read, the postcondition and effect re-proof — happens
        inside :func:`execute_remote_attended_action`, unchanged.
        """
        relay = decision.relay
        request = RemoteAttendedActionRequest(
            capability_digest=str(relay["capability_digest"]),
            idempotency_key=str(relay["idempotency_key"]),
            action=str(relay["action"]),  # type: ignore[arg-type]
            disposition=_ENGINE_DISPOSITION[str(relay["action"])],  # type: ignore[arg-type]
            task_digest=str(relay["task_digest"]),
            task_signature=str(relay["task_signature"]),
            tenant_id=self._tenant_id,
            runner_id=self._runner_id,
            phase="paused",
            event_sequence=int(relay["event_sequence"]),
            idempotency_scope_digest=str(relay["idempotency_scope_digest"]),
            binding_digest=str(relay["binding_digest"]),
        )
        principal = RemoteDecisionPrincipal(
            subject=str(relay["actor_id"]),
            tenant_id=self._tenant_id,
            runner_id=self._runner_id,
            assurance="aal2",
        )
        return execute_remote_attended_action(
            run_dir,
            item,
            request,
            deployment=self._deployment,
            principal=principal,
            executor=executor,
            relay_binding=decision.durable_binding(),
            key=key,
        )

    def acknowledge(
        self,
        decision: RelayedDecision,
        result: str,
        *,
        timeout_s: float = 15.0,
    ) -> bool:
        """Tell the control plane what the engine did with one decision.

        Returns:
            ``True`` when the acknowledgement was accepted. ``False`` when it
            may or may not have been recorded — the decision stays leased
            server-side and will be re-delivered, and the engine's idempotency
            key makes that replay a no-op rather than a second execution.
        """
        if result not in _ACK_RESULTS:
            raise RelayRefused(f"unknown relay acknowledgement result {result!r}")
        try:
            status, body = self._transport.post(
                _ack_path(decision.decision_id),
                {
                    "result": result,
                    "relay_digest": decision.relay_digest,
                    "relay_signature": decision.relay_signature,
                },
                timeout_s=timeout_s,
            )
        except RelayUncertain:
            return False
        if status >= 500:
            return False
        if status >= 400:
            raise RelayRefused("the control plane refused the acknowledgement")
        return body.get("accepted") is True

    # -- one full cycle ---------------------------------------------------

    def serve_once(
        self,
        run_dir: Path,
        item: AttentionItem,
        *,
        wait_s: float = 25.0,
        executor: Optional[AttendedActionExecutor] = None,
    ) -> Optional[AttendedDecision]:
        """Poll once, and if a decision is waiting, execute and acknowledge it.

        A governed refusal is acknowledged as ``refused`` rather than swallowed,
        so the hosted surface can tell the operator their answer was not
        accepted instead of leaving them looking at a decision that appears to
        have been taken.
        """
        decision = self.poll(wait_s=wait_s)
        if decision is None:
            return None
        try:
            outcome = self.execute(run_dir, item, decision, executor=executor)
        except AttendedActionRefused:
            self.acknowledge(decision, "refused")
            raise
        self.acknowledge(
            decision,
            "accepted" if outcome.status != "refused" else "refused",
        )
        return outcome
