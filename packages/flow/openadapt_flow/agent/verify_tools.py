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
        #: Held in this server's memory only: after a restart, recheck from
        #: Python with the saved ``recheck.json`` (``RecheckState.load``).
        self._rechecks: dict[str, tuple[PendingWrite, WriteResult]] = {}
        self._lock = threading.Lock()

    # -- the MCP layer's interface ------------------------------------------

    def handles(self, name: str) -> bool:
        return name in VERIFY_TOOLS

    def hold_needed(self, arguments: Mapping[str, Any]) -> None:
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
                    "Before the due time it returns due: false and the time to "
                    "call again."
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
        except AlreadyWritten as exc:
            return {
                "ok_to_write": False,
                "reason": f"{exc}; don't make the write",
                "earlier_result": exc.result,
            }
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
        result = verify_write(
            entry, write_attempted=bool(args.get("write_attempted", True))
        )
        out = result.to_dict()
        if result.recheck is not None and out.get("provisional"):
            with self._lock:
                self._rechecks[write_id] = (entry, result)
            out["next"] = (
                "Provisional: call verify_recheck with this write_id at or after "
                "recheck_due_at. A write that lands before then can still refute "
                "it; a write after the recheck is not seen."
            )
        return out

    def _recheck(self, args: Mapping[str, Any]) -> dict[str, Any]:
        write_id = str(args.get("write_id"))
        with self._lock:
            held = self._rechecks.get(write_id)
        if held is None:
            raise BridgeError(
                "no provisional result for that write_id: only a CONFIRMED write "
                "whose contract sets frame.database.recheck_after_s has a "
                "recheck, each is rechecked once, and this server holds them in "
                "memory only"
            )
        entry, result = held
        try:
            late = recheck(
                entry.contract, result, params=entry.params, actor=entry.actor
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
        except VerifierError as exc:
            with self._lock:
                self._rechecks.pop(write_id, None)
            raise BridgeError(f"the write can't be rechecked: {exc}") from exc
        with self._lock:
            self._rechecks.pop(write_id, None)
        if late.verified:
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
            "outcome": late.outcome,
            "late": late.late,
            "reasons": list(late.reasons),
            "recheck_due_at": late.due_at,
            "read_at": late.read_at,
            "receipt_sha256": late.receipt_sha256,
            "next": nxt,
        }
