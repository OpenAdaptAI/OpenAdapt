"""The pre-commit gate: allow, hold or refuse one commit-class action.

An executor wrapper (:mod:`openadapt_flow.flight.computer`) calls
:meth:`PreCommitGate.decide` before it performs a click, double click or
Enter whose target reads as a commit verb (Save, Submit, Send...) or can't be
read at all. ``allow`` performs the action. ``hold`` and ``refuse`` both
leave it undone and tell the agent so; ``hold`` means a person may still
decide (an approval request is open), ``refuse`` means a person or the gate
said no.

Three gates ship here:

- :class:`TerminalGate` asks ``y/N`` on the controlling terminal and refuses
  when there is none.
- :class:`FileGate` writes an item-approval request
  (``openadapt.item-approval-request/v1``) in the run directory, so Desktop's
  Run it or a paired phone can answer, and waits for a person's signed answer.
- :class:`RefuseGate` never allows (unattended runs).

Every error in a gate means the action is not performed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional, Protocol, runtime_checkable

__all__ = [
    "Decision",
    "FileGate",
    "GateAction",
    "PreCommitGate",
    "RefuseGate",
    "TerminalGate",
    "HELD_MESSAGE",
]

Decision = Literal["allow", "hold", "refuse"]
DECISIONS: tuple[str, ...] = ("allow", "hold", "refuse")

#: What the agent is told when an action was not performed.
HELD_MESSAGE = (
    "held for a person: not performed. This action looks like it commits a "
    "change (target {label}), so a person has to approve it first. Do not "
    "retry it; continue with other work or stop."
)


@dataclass(frozen=True)
class GateAction:
    """One commit-class action, as the gate sees it.

    ``raw`` is the executor's own action input. ``label`` is the text read
    under the pointer (None when nothing could be read); ``label_source`` is
    ``accessibility``, ``ocr`` or ``none``. ``reason`` says why the action is
    commit-class.
    """

    dialect: str
    raw: dict[str, Any]
    kind: str
    x: Optional[int]
    y: Optional[int]
    label: Optional[str]
    label_source: str
    reason: str
    index: int = 0
    ops: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def describe(self) -> str:
        where = f" at ({self.x}, {self.y})" if self.x is not None else ""
        target = repr(self.label) if self.label else "an unreadable target"
        return f"{self.kind}{where} on {target}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "dialect": self.dialect,
            "raw": self.raw,
            "kind": self.kind,
            "x": self.x,
            "y": self.y,
            "label": self.label,
            "label_source": self.label_source,
            "reason": self.reason,
            "index": self.index,
            "ops": list(self.ops),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GateAction":
        return cls(
            dialect=str(data.get("dialect", "")),
            raw=dict(data.get("raw") or {}),
            kind=str(data.get("kind", "")),
            x=data.get("x"),
            y=data.get("y"),
            label=data.get("label"),
            label_source=str(data.get("label_source", "none")),
            reason=str(data.get("reason", "")),
            index=int(data.get("index", 0)),
            ops=tuple(data.get("ops") or ()),
        )


@runtime_checkable
class PreCommitGate(Protocol):
    """Decide one commit-class action. Never performs it."""

    def decide(self, action: GateAction, screenshot: Optional[bytes]) -> Decision: ...


class RefuseGate:
    """Never allows. For unattended runs: every commit-class action is held."""

    def __init__(self, decision: Decision = "hold") -> None:
        if decision not in ("hold", "refuse"):
            raise ValueError("RefuseGate only holds or refuses")
        self._decision: Decision = decision

    def decide(self, action: GateAction, screenshot: Optional[bytes]) -> Decision:
        return self._decision


class TerminalGate:
    """Ask ``y/N`` on the controlling terminal; refuse without one.

    It opens ``/dev/tty`` itself, so it works while the agent owns stdin and
    stdout. Anything but ``y`` or ``yes`` refuses.
    """

    def __init__(self, tty_path: str = "/dev/tty") -> None:
        self._tty_path = tty_path

    def decide(self, action: GateAction, screenshot: Optional[bytes]) -> Decision:
        try:
            with open(self._tty_path, "r+", encoding="utf-8") as tty:
                tty.write(
                    f"\n[openadapt flight] The agent wants to {action.describe()}.\n"
                    f"  Why it is held: {action.reason}\n"
                    "  Allow it? [y/N] "
                )
                tty.flush()
                answer = tty.readline().strip().lower()
        except OSError:
            return "refuse"
        return "allow" if answer in ("y", "yes") else "refuse"


class FileGate:
    """Ask through an item-approval request that Desktop or a phone answers.

    Each decision writes ``item-approval/request-<id>.json`` and the frame in
    ``run_dir`` (the existing request format; the frame is local only, as for
    every approval request) and waits up to ``timeout_s`` for a person's
    signed answer. An approval is used once. No answer in time is ``hold``;
    a decline, or an answer the run refuses, is ``refuse``.
    """

    def __init__(
        self,
        run_dir: Path | str,
        *,
        run_id: str,
        workflow_name: str = "flight",
        actor: str = "agent:flight",
        timeout_s: float = 300.0,
        poll_s: float = 0.5,
        key_dir: Optional[Path | str] = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.workflow_name = workflow_name
        self.actor = actor
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self.key_dir = key_dir
        self._store: Any = None
        self.requests: list[str] = []

    def _open_store(self) -> Any:
        from openadapt_flow.approval_mode.approver import DEFAULT_APPROVER_POLICY
        from openadapt_flow.approval_mode.item_approval import (
            ItemApprovalRefused,
            ItemApprovalStore,
        )

        if self._store is None:
            store = ItemApprovalStore(self.run_dir, key_dir=self.key_dir)
            # Person-only, pinned before anything is asked: no delegate grant
            # can let an agent answer its own save.
            store.write_policy(DEFAULT_APPROVER_POLICY)
            store.write_initiator(self.actor)
            try:
                store.write_approver_root()
            except ItemApprovalRefused:
                # No verifiable roster: the question is still asked, and no
                # answer to it counts (fails closed).
                pass
            self._store = store
        return self._store

    def decide(self, action: GateAction, screenshot: Optional[bytes]) -> Decision:
        try:
            return self._decide(action, screenshot)
        except Exception:  # noqa: BLE001 - any failure leaves the action undone
            return "refuse"

    def _decide(self, action: GateAction, screenshot: Optional[bytes]) -> Decision:
        from openadapt_flow.approval_mode.approver import StepClass
        from openadapt_flow.approval_mode.item_approval import wait_for_answer
        from openadapt_flow.approval_mode.model import ApprovalMode

        if not screenshot:
            # The person must see the screen they approve.
            return "refuse"
        store = self._open_store()
        state = json.dumps(
            {"action": action.raw, "label": action.label}, sort_keys=True, default=str
        )
        request = store.open_request(
            run_id=self.run_id,
            workflow_name=self.workflow_name,
            step_id=f"action-{action.index}",
            step_index=action.index,
            step_intent=f"{action.describe()} ({action.reason})",
            mode=ApprovalMode.EACH_ITEM,
            bundle_content_digest=None,
            runtime_inputs_digest=None,
            frame_png=screenshot,
            form_state=(
                "screen_text",
                "sha256:" + hashlib.sha256(state.encode("utf-8")).hexdigest(),
            ),
            ttl_s=max(self.timeout_s, 60.0),
            step_class=StepClass.CONSEQUENTIAL,
            provenance={"actor": self.actor[:128], "reason": action.reason[:512]},
        )
        self.requests.append(request.request_id)
        answer, code = wait_for_answer(
            store, request, timeout_s=self.timeout_s, poll_s=self.poll_s
        )
        if answer is None:
            return (
                "hold"
                if code in ("approval_wait_timed_out", "request_expired")
                else "refuse"
            )
        if answer.decision != "approve":
            return "refuse"
        if not store.consume(request.request_id):
            return "refuse"
        return "allow"
