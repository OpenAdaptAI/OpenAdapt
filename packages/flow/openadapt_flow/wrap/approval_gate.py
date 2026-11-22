"""A pre-commit gate a person answers from a paired phone or the CLI.

:class:`~openadapt_flow.wrap.fallback.TerminalGate` needs a person at the
terminal, so a fallback started by a service (no TTY) never starts the
agent. :class:`ItemApprovalGate` asks through the item-approval store that
``each_item`` runs and the agent guard use
(:mod:`openadapt_flow.approval_mode.item_approval`): before each save the
fallback run writes ``item-approval/request-<id>.json`` with the gate
screenshot, and waits. The decision portal on a paired phone (when its runs
root holds the approvals folder) or ``openadapt-flow item-approval approve``
with the person's approver key writes the answer. Desktop's Run it screen
lists only runs Desktop started, so it does not show these requests.

The gate lets the save through only for an answer that
:func:`~openadapt_flow.approval_mode.item_approval.answer_refusal` accepts:
decided by a person, signed by an approver key on the pinned approver roster,
for this exact request, before the request expired, and not by the agent
that asked. Anything else, including no answer within ``wait_s``, a decline,
an unsigned or off-roster answer and a missing screenshot, refuses the save.
With no approver root pinned, :attr:`ItemApprovalGate.attended` is False and
the fallback agent is never started.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.compile_agent.harness import GateRequest

#: The run id prefix the gate's requests carry (the fallback request id).
WORKFLOW_NAME = "wrap-fallback"
#: Default wait for a person, in seconds. The agent's own run ceiling (at
#: most 120 seconds for a provider agent) still bounds the whole run.
DEFAULT_WAIT_S = 60.0


def pin_from_config(config: Optional[str]) -> dict[str, Any]:
    """The approver pin a deployment config's ``approvers`` section names.

    ``{}`` without a config. A config that can't be read raises
    ``ValueError``: it must not quietly drop its pin.
    """

    if not config:
        return {}
    from openadapt_flow.deployment import load_deployment

    try:
        section = load_deployment(config, allow_unresolved=True).approvers
    except (OSError, ValueError) as exc:
        raise ValueError(f"deployment config {config} can't be read: {exc}") from exc
    if section is None:
        return {}
    pin: dict[str, Any] = {"config_root": section.root_public_key}
    if section.head:
        pin["config_head"] = tuple(section.head)
    if section.key_dir:
        pin["key_dir"] = Path(section.key_dir).expanduser().resolve()
    return pin


class ItemApprovalGate:
    """Ask a person to approve each fallback save through the item-approval
    store, and wait for a signed answer.

    Args:
        approvals_dir: Where each fallback run's approval folder goes
            (``<approvals_dir>/<fallback request id>/item-approval``), for
            a decision portal that scans that runs root. Default: inside the
            fallback run's own directory.
        wait_s: How long to wait for each answer. The request expires then,
            so a later answer is refused.
        key_dir: The approver key directory (default: the admission key
            directory, ``$OPENADAPT_JIT_KEY_DIR`` or ``~/.openadapt/jit``).
        approver_root, config_root, config_head, config_only: The approver
            pin, as :class:`ItemApprovalStore` takes it.
        agent_id: Who asks; recorded as the run's initiator, so it can never
            answer.
    """

    def __init__(
        self,
        *,
        approvals_dir: Optional[Path | str] = None,
        wait_s: float = DEFAULT_WAIT_S,
        key_dir: Optional[Path | str] = None,
        approver_root: Optional[str] = None,
        config_root: Optional[str] = None,
        config_head: Optional[tuple[int, str]] = None,
        config_only: bool = False,
        agent_id: str = "wrap-fallback",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)):
            raise ValueError("wait_s is a number of seconds")
        if not 0 < float(wait_s) <= 4 * 3600:
            raise ValueError("wait_s is more than 0 and at most 4 hours")
        self.approvals_dir = (
            Path(approvals_dir).expanduser() if approvals_dir is not None else None
        )
        self.wait_s = float(wait_s)
        self.key_dir = Path(key_dir) if key_dir is not None else None
        self._pin: dict[str, Any] = (
            {
                "config_root": config_root,
                "config_only": True,
                "config_head": config_head,
            }
            if config_only
            else {
                "approver_root": approver_root,
                "config_root": config_root,
                "config_head": config_head,
            }
        )
        self.agent_id = agent_id
        self._sleep = sleep
        self._dir: Optional[Path] = None
        self._run_id: Optional[str] = None
        self._store: Any = None
        self._asked = 0
        #: One row per save asked: request id and digest, and the decision
        #: or the closed refusal code. PHI-free.
        self.decisions: list[dict[str, Any]] = []

    # -- whether anyone can answer ---------------------------------------------

    def _root(self) -> Optional[str]:
        from openadapt_flow.approval_mode.approver_roster import (
            RosterError,
            pinned_root,
            verified_approvers,
        )
        from openadapt_flow.jit.lifecycle import default_key_dir

        try:
            root = pinned_root(
                self._pin.get("approver_root"),
                config_root=self._pin.get("config_root"),
                config_only=bool(self._pin.get("config_only")),
            )
            if root is None:
                return None
            verified_approvers(
                self.key_dir or default_key_dir(),
                root,
                at_least=self._pin.get("config_head"),
            )
        except RosterError:
            return None
        return root

    @property
    def attended(self) -> bool:
        """True only when a pinned approver roster verifies: otherwise every
        person's answer would be refused, so the agent is not started."""

        return self._root() is not None

    def bind(self, run_root: Path, request_id: str) -> None:
        """The fallback run this gate serves (FallbackPolicy calls it)."""

        self._dir = (
            self.approvals_dir / request_id
            if self.approvals_dir is not None
            else Path(run_root) / "approval"
        )
        self._run_id = request_id
        self._store = None
        self._asked = 0
        self.decisions = []

    # -- one save ----------------------------------------------------------------

    def _open_store(self) -> Any:
        from openadapt_flow.approval_mode.approver import DEFAULT_APPROVER_POLICY
        from openadapt_flow.approval_mode.item_approval import ItemApprovalStore

        assert self._dir is not None
        store = ItemApprovalStore(self._dir, key_dir=self.key_dir, **self._pin)
        # Person-only, pinned before anything is asked; the agent that asks
        # is recorded so it can never answer.
        store.write_policy(DEFAULT_APPROVER_POLICY)
        store.write_initiator(f"agent:{self.agent_id}")
        store.write_approver_root()
        return store

    def _refuse(self, code: str, **row: Any) -> bool:
        self.decisions.append({"decision": "refused", "code": code, **row})
        return False

    def __call__(self, request: GateRequest) -> bool:
        from openadapt_flow.approval_mode.item_approval import (
            ItemApprovalRefused,
            wait_for_answer,
        )
        from openadapt_flow.approval_mode.model import ApprovalMode

        if self._dir is None or self._run_id is None:
            return self._refuse("gate_not_bound")
        shot = request.screenshot
        try:
            if shot is None or Path(shot).is_symlink():
                raise OSError("no screenshot")
            frame = Path(shot).read_bytes()
        except OSError:
            # A person approves what they see; with nothing to show, refuse.
            return self._refuse("screenshot_required")
        if not frame:
            return self._refuse("screenshot_required")
        identity = [str(s) for s in request.identity]
        facts = json.dumps(
            {"control": request.control, "identity": identity, "url": request.url},
            sort_keys=True,
        ).encode("utf-8")
        index = self._asked
        self._asked += 1
        try:
            if self._store is None:
                self._store = self._open_store()
            store = self._store
            asked = store.open_request(
                run_id=self._run_id,
                workflow_name=WORKFLOW_NAME,
                step_id=f"save-{index + 1}",
                step_index=index,
                # The control only: the record's on-screen identity stays in
                # the screenshot, not in text that lists and summaries show.
                step_intent=f"Press '{request.control}' (agent fallback save)",
                mode=ApprovalMode.EACH_ITEM,
                bundle_content_digest=None,
                runtime_inputs_digest=None,
                frame_png=frame,
                form_state=(
                    "screen_text",
                    "sha256:" + hashlib.sha256(facts).hexdigest(),
                ),
                ttl_s=self.wait_s,
                provenance={
                    "actor": f"agent:{self.agent_id}",
                    "task_scope": "the task's own record, after the compiled "
                    "program stopped before saving",
                },
            )
        except ItemApprovalRefused as exc:
            return self._refuse(exc.code)
        except Exception:  # noqa: BLE001 - no request a person can answer
            return self._refuse("request_refused")
        row = {"request_id": asked.request_id, "request_sha256": asked.digest()}
        answer, code = wait_for_answer(
            store, asked, timeout_s=self.wait_s, sleep=self._sleep
        )
        if answer is None:
            return self._refuse(code or "no_answer", **row)
        if answer.decision != "approve":
            return self._refuse("declined", **row)
        if not store.consume(asked.request_id):
            return self._refuse("approval_already_used", **row)
        self.decisions.append(
            {
                "decision": "approved",
                "answer_sha256": answer.digest(),
                "route": answer.route,
                "signer": answer.attestation.signer if answer.attestation else None,
                **row,
            }
        )
        return True
