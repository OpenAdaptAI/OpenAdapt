"""``openadapt verify``: MCP tools any agent calls around its own writes.

The operator starts the server with a folder of write contracts
(``<name>.yaml``, ``openadapt.verify/v1``): what to read in the system of
record, and what must be true there after each kind of write. An agent can't
change them. Around each write it makes, in whatever way it makes it, the
agent calls:

``verify_contracts``
    The contracts this server checks, with the values each one needs.
``verify_begin``
    Names a contract and the values the write will save. The server reads
    the system of record first, so a new record can be told from an old one.
    If the store can't be read, the agent must not write.
``verify_check``
    After the write. The server reads the store again and returns
    ``verified: true`` with a receipt, or a refusal: ``REFUTED`` (the store
    shows a different write) or ``RECONCILIATION_REQUIRED`` (it doesn't show
    the write, which may still land); ``outcome`` says the same as
    ``CONFIRMED``, ``REFUTED`` or ``UNCERTAIN``. Never retry a refused write:
    a person checks the record. When the contract sets
    ``frame.database.recheck_after_s``, a ``CONFIRMED`` result also says
    ``provisional: true`` and ``recheck_due_at``: a write that lands before
    then can still refute it.
``verify_recheck``
    At or after ``recheck_due_at``. The server reads the whole database
    again and returns ``CONFIRMED`` (the earlier verdict stands), ``REFUTED``
    (something moved since that nothing explains: the earlier ``CONFIRMED``
    no longer stands) or ``UNCERTAIN``. A write after this read is not seen.
    An owed recheck is also in the uncertain-write ledger, so ``openadapt
    verify ledger list`` shows it after a restart, until a recheck settles it.

``verify_begin`` consults the uncertain-write ledger, so a write whose
outcome is unknown is refused, also after a restart or in another server.
No model is called. See :mod:`openadapt_flow.verifier`.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Mapping, Optional

from openadapt_flow.agent.bridge import BridgeError, ToolSpec
from openadapt_flow.runtime.effects.late_write import parse_time
from openadapt_flow.verifier import (
    Actor,
    AlreadyWritten,
    Contract,
    PendingWrite,
    RecheckNotDue,
    VerifierError,
    WriteBlocked,
    WriteResult,
    begin_write,
    recheck,
    verify_write,
)
from openadapt_flow.write_ledger import LedgerIntegrityHeld, default_ledger

__all__ = ["VERIFY_TOOLS", "VerifyBridge", "contract_params"]

VERIFY_TOOLS = ("verify_contracts", "verify_begin", "verify_check", "verify_recheck")
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_WRITE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")

INSTRUCTIONS = (
    "This server verifies writes you make to a system of record, against "
    "contracts the operator set. Around each write: call verify_begin with "
    "the contract and the values you will save; if it refuses, don't write. "
    "Make the write your own way. Then call verify_check with the write_id. "
    "Only verified: true means the write is in the system of record as "
    "declared. If the result also says provisional: true, call "
    "verify_recheck with the write_id at or after recheck_due_at: a write that "
    "lands before then can still refute it. A refusal means a person must "
    "check the record: never retry that write, and tell the person the "
    "verdict and reason."
)


def contract_params(contract: Contract) -> list[str]:
    """The ``{"param": name}`` placeholders a contract binds."""

    return contract.params


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


class VerifyBridge:
    """The verify tools for :func:`openadapt_flow.agent.mcp.build_server`."""

    instructions = INSTRUCTIONS
    taught: dict[str, str] = {}

    def __init__(self, contracts_dir: Path, state_dir: Path) -> None:
        self.contracts_dir = Path(contracts_dir).expanduser().resolve()
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.contracts: dict[str, Contract] = {}
        for path in sorted(self.contracts_dir.glob("*.y*ml")):
            if not _NAME.fullmatch(path.stem):
                raise ValueError(
                    f"contract file name {path.name!r}: use a-z, 0-9, _ and -"
                )
            self.contracts[path.stem] = Contract.load(path)
        if not self.contracts:
            raise ValueError(f"no write contracts (*.yaml) in {self.contracts_dir}")
        self._pending: dict[str, PendingWrite] = {}
        #: Provisional results whose recheck is still to run, by write_id.
        #: This server can run them only while it holds them; the ledger
        #: records each one owed (``openadapt verify ledger list``), and
        #: after a restart it is rechecked from Python with the saved
        #: ``recheck.json`` (``RecheckState.load``).
        self._rechecks: dict[str, tuple[PendingWrite, WriteResult]] = {}
        #: Rechecks this server already ran, by write_id: their outcome.
        self._spent: dict[str, str] = {}
        #: Every checked write with a recheck state, of any outcome.
        self._checked: list[tuple[PendingWrite, WriteResult]] = []
        self._lock = threading.Lock()

    # -- the MCP layer's interface ------------------------------------------

    def handles(self, name: str) -> bool:
        return name in VERIFY_TOOLS

    def hold_needed(
        self, arguments: Mapping[str, Any], tool: str = "teach_act"
    ) -> None:
        """Always None: this bridge has no person holds. ``tool`` is ignored."""

        return None

    def list_tool_specs(self) -> list[ToolSpec]:
        names = sorted(self.contracts)
        return [
            ToolSpec(
                name="verify_contracts",
                description=(
                    "The write contracts this server checks, and the values each "
                    "one needs in verify_begin."
                ),
                input_schema=_obj({}, []),
                annotations={"readOnlyHint": True, "openWorldHint": False},
            ),
            ToolSpec(
                name="verify_begin",
                description=(
                    "Call before a write. Names the contract and the values the "
                    "write will save; the server reads the system of record first. "
                    "Returns write_id. If it refuses, don't make the write."
                ),
                input_schema=_obj(
                    {
                        "contract": {"type": "string", "enum": names},
                        "values": {
                            "type": "object",
                            "additionalProperties": {
                                "type": ["string", "number", "boolean"]
                            },
                        },
                        "write_key": {
                            "type": "string",
                            "pattern": _WRITE_KEY.pattern,
                            "description": (
                                "Your id for this one write (a task or ticket "
                                "id). With it, a retry of the write is never "
                                "sent twice, however late it comes."
                            ),
                        },
                        "agent_id": {
                            "type": "string",
                            "pattern": _AGENT_ID.pattern,
                            "description": "Your agent's name, for the receipt.",
                        },
                    },
                    ["contract", "values"],
                ),
                annotations={"readOnlyHint": True, "openWorldHint": True},
            ),
            ToolSpec(
                name="verify_check",
                description=(
                    "Call after the write. Reads the system of record again and "
                    "returns verified: true with a receipt, or a refusal (REFUTED "
                    "or RECONCILIATION_REQUIRED). Never retry a refused write. "
                    "Set write_attempted false only if you know the write was "
                    "never sent."
                ),
                input_schema=_obj(
                    {
                        "write_id": {"type": "string", "maxLength": 64},
                        "write_attempted": {"type": "boolean"},
                    },
                    ["write_id"],
                ),
                annotations={"readOnlyHint": True, "openWorldHint": True},
            ),
            ToolSpec(
                name="verify_recheck",
                description=(
                    "Call at or after a provisional result's recheck_due_at. "
                    "Reads the whole database again: CONFIRMED (the verdict "
                    "stands), REFUTED (something moved after it that nothing "
                    "explains: the write is no longer verified) or UNCERTAIN. "
                    "Your later writes under the same contract and agent_id, "
                    "checked with verify_check, account for their own rows; a "
                    "write under another contract or another agent_id does "
                    "not, and refutes it. Before the due time it returns due: "
                    "false and the time to call again."
                ),
                input_schema=_obj(
                    {"write_id": {"type": "string", "maxLength": 64}}, ["write_id"]
                ),
                annotations={"readOnlyHint": True, "openWorldHint": True},
            ),
        ]

    def dispatch(
        self,
        name: str,
        arguments: Optional[Mapping[str, Any]] = None,
        **_: Any,
    ) -> dict[str, Any]:
        args = dict(arguments or {})
        if name == "verify_contracts":
            return {
                "contracts": [
                    {"contract": n, "values": contract_params(c)}
                    for n, c in sorted(self.contracts.items())
                ]
            }
        if name == "verify_begin":
            return self._begin(args)
        if name == "verify_check":
            return self._check(args)
        if name == "verify_recheck":
            return self._recheck(args)
        raise BridgeError("unknown tool name")

    # -- tools ------------------------------------------------------------------

    def _begin(self, args: Mapping[str, Any]) -> dict[str, Any]:
        contract = self.contracts.get(str(args.get("contract")))
        if contract is None:
            raise BridgeError(f"no contract named {args.get('contract')!r}")
        values = {str(k): v for k, v in (args.get("values") or {}).items()}
        missing = [p for p in contract_params(contract) if p not in values]
        if missing:
            raise BridgeError(f"values missing for: {', '.join(missing)}")
        agent_id = str(args.get("agent_id") or "mcp-agent")
        write_key = args.get("write_key")
        if write_key is not None and not _WRITE_KEY.fullmatch(str(write_key)):
            raise BridgeError("write_key: 1-128 of A-Za-z0-9._:@/-")
        try:
            pending = begin_write(
                contract,
                values,
                actor=Actor("agent", agent_id),
                workdir=self.state_dir,
                write_key=None if write_key is None else str(write_key),
            )
        except LedgerIntegrityHeld as exc:
            return {
                "ok_to_write": False,
                "reason": "the write ledger is held; don't make the write",
                "hold_reason": exc.code,
            }
        except AlreadyWritten as exc:
            out: dict[str, Any] = {
                "ok_to_write": False,
                "reason": f"{exc}; don't make the write",
                "earlier_result": exc.result,
            }
            if exc.result.get("provisional"):
                out["next"] = self._owed_next(str(exc.result.get("write_id") or ""))
            return out
        except WriteBlocked as exc:
            return {"ok_to_write": False, "reason": f"{exc}; don't make the write"}
        with self._lock:
            self._pending[pending.write_id] = pending
        return {
            "ok_to_write": True,
            "write_id": pending.write_id,
            "next": "Make the write, then call verify_check with this write_id.",
        }

    def _check(self, args: Mapping[str, Any]) -> dict[str, Any]:
        write_id = str(args.get("write_id"))
        with self._lock:
            entry = self._pending.pop(write_id, None)
        if entry is None:
            raise BridgeError(
                "no pending write with that write_id (each one is checked once)"
            )
        try:
            result = verify_write(
                entry, write_attempted=bool(args.get("write_attempted", True))
            )
        except LedgerIntegrityHeld as exc:
            return {
                "verified": False,
                "outcome": "UNCERTAIN",
                "retry_safe": False,
                "hold_reason": exc.code,
            }
        out = result.to_dict()
        if result.recheck is not None:
            # Any outcome: a later write's own window excuses its rows in an
            # earlier write's recheck (``excused_by``).
            with self._lock:
                self._checked.append((entry, result))
        if result.recheck is not None and out.get("provisional"):
            with self._lock:
                self._rechecks[write_id] = (entry, result)
            out["next"] = (
                "Provisional: call verify_recheck with this write_id at or after "
                "recheck_due_at. A write that lands before then can still refute "
                "it; a write after the recheck is not seen."
            )
        return out

    def _owed_next(self, write_id: str) -> str:
        """What a retry handed a provisional result should do next."""

        with self._lock:
            held = write_id in self._rechecks
        if held:
            return (
                "The earlier result is provisional: call verify_recheck with "
                f"write_id {write_id} at or after recheck_due_at."
            )
        return (
            "The earlier result is provisional and its recheck is owed, but "
            "this server doesn't hold it (it restarted, or another server "
            "checked the write): a person rechecks it from the saved "
            "recheck.json. openadapt verify ledger list shows it until then."
        )

    def _unheld(self, write_id: str) -> str:
        """Why this server has no recheck to run for ``write_id``."""

        with self._lock:
            spent = self._spent.get(write_id)
        if spent is not None:
            return (
                f"that write was already rechecked ({spent}): each write is "
                "rechecked once"
            )
        owed = None
        try:
            found = default_ledger().find_attempt(write_id)
            if found is not None:
                owed = found[1].recheck_owed
        except LedgerIntegrityHeld as exc:
            return f"the write ledger is held ({exc.code})"
        except Exception:  # noqa: BLE001 - only the wording depends on it
            owed = None
        if owed:
            return (
                "no provisional result for that write_id in this server, but "
                f"the ledger shows its recheck owed (due {owed}): the server "
                "restarted, or another server checked the write. A person "
                "rechecks it from the saved recheck.json; openadapt verify "
                "ledger list shows it until then"
            )
        return (
            "no provisional result for that write_id: only a CONFIRMED write "
            "whose contract sets frame.database.recheck_after_s has a recheck"
        )

    def _later_writes(
        self, entry: PendingWrite, result: WriteResult
    ) -> list[WriteResult]:
        """This session's checked writes under the same contract by the same
        actor, begun after ``result``'s post-save read: each excuses only
        what changed in its own window. A write under another contract, or
        one begun before that read, excuses nothing."""

        state = result.recheck
        if state is None:
            return []
        post = state.payload.get("post_state") or {}
        try:
            since = parse_time(str(post.get("read_at")))
        except ValueError:
            return []
        out: list[WriteResult] = []
        for other_entry, other in self._checked:
            if other is result or other_entry.contract is not entry.contract:
                continue
            if other_entry.actor != entry.actor or other.recheck is None:
                continue
            began = (other.recheck.payload.get("baseline") or {}).get(
                "baseline_read_at"
            )
            try:
                if parse_time(str(began)) < since:
                    continue
            except ValueError:
                continue
            out.append(other)
        return out

    def _recheck(self, args: Mapping[str, Any]) -> dict[str, Any]:
        write_id = str(args.get("write_id"))
        with self._lock:
            held = self._rechecks.get(write_id)
        if held is None:
            raise BridgeError(self._unheld(write_id))
        entry, result = held
        with self._lock:
            unchecked = [
                p
                for p in self._pending.values()
                if p.contract is entry.contract and p.actor == entry.actor
            ]
            later = self._later_writes(entry, result)
        if unchecked:
            # Its rows would look unexplained: no verdict until it is checked.
            raise BridgeError(
                "a later write under this contract is not checked yet; call "
                "verify_check for it, then verify_recheck again"
            )
        try:
            late = recheck(
                entry.contract,
                result,
                params=entry.params,
                actor=entry.actor,
                excused_by=later,
            )
        except RecheckNotDue as exc:
            return {
                "due": False,
                "write_id": write_id,
                "recheck_due_at": exc.due_at,
                "wait_s": max(1, round(exc.wait_s)),
                "next": "Not due yet: call verify_recheck again at or after "
                "recheck_due_at.",
            }
        except LedgerIntegrityHeld as exc:
            return {
                "write_id": write_id,
                "verified": False,
                "outcome": "UNCERTAIN",
                "final": False,
                "recheck_owed": True,
                "hold_reason": exc.code,
            }
        except VerifierError as exc:
            # Nothing was judged: the recheck is still owed and held here.
            raise BridgeError(
                f"the recheck couldn't run: {exc}; it is still owed, so call "
                "verify_recheck again once that is fixed"
            ) from exc
        if not late.recheck_owed:
            with self._lock:
                self._rechecks.pop(write_id, None)
                self._spent[write_id] = late.outcome
        if late.recheck_owed:
            nxt = (
                "The recheck remains owed: call verify_recheck again at or "
                "after recheck_due_at. Don't retry the write."
            )
        elif late.verified:
            nxt = "The verdict stands. A write after read_at is not seen."
        elif late.late:
            nxt = (
                "The earlier CONFIRMED no longer stands. Don't retry: a person "
                "checks the record."
            )
        else:
            nxt = "Not a pass. Don't retry: a person checks the record."
        return {
            "due": True,
            "write_id": write_id,
            "verified": late.verified,
            **(
                {
                    "hold_reason": late.hold_reason,
                    "current_observation": late.current_observation,
                }
                if late.hold_reason is not None
                else {}
            ),
            "outcome": late.outcome,
            "late": late.late,
            "final": late.final,
            "reasons": list(late.reasons),
            "recheck_due_at": late.due_at,
            "read_at": late.read_at,
            "receipt_sha256": late.receipt_sha256,
            "recheck_claim_sha256": late.recheck_claim_sha256 or None,
            "excuse_receipt_sha256s": list(late.excuse_receipt_sha256s)
            if late.excuse_receipt_sha256s is not None
            else None,
            "recheck_owed": late.recheck_owed,
            "next": nxt,
        }
