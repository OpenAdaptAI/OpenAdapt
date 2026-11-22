"""Pre-check, read-back and receipt for one coding agent's tool calls.

Each hook event is its own process, so a write's state between its
``PreToolUse`` and ``PostToolUse`` lives in files under
``.openadapt/hooks/state`` (mode 0700; the baseline snapshot, which holds raw
records, is mode 0600). The read-back is :mod:`openadapt_flow.verifier`'s
file-based pair: :func:`~openadapt_flow.verifier.capture` before the call,
a saved :class:`~openadapt_flow.verifier.Snapshot`, and
:func:`~openadapt_flow.verifier.verify_write` after it. That pair doesn't use
the uncertain-write ledger (its lease lives in one process), so the hooks
keep their own hold: an identical call after an ``UNCERTAIN`` or
``REFUTED`` result is refused until a person releases it.

Outcomes, as the agent and the person see them:

* ``VERIFIED``: the record reads back as declared, and nothing else in the
  read set changed (the verifier's ``CONFIRMED``).
* ``REFUTED``: the store shows a different write. Don't retry.
* ``UNCERTAIN``: the read-back failed, showed nothing, or couldn't run.
  The write may still land. Never a pass.
* ``BLOCKED``: the pre-check refused the call; nothing was sent.

Every outcome gets a run folder under ``.openadapt/hooks/runs/<id>/`` with
``report.json`` and a signed ``record/run/v1`` ``receipt.json``
(:mod:`openadapt_flow.receipt_record`).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

from openadapt_flow.hooks import HOOKS_DIR, SYSTEMS_FILE
from openadapt_flow.hooks.agents import HookInput, HookOutput
from openadapt_flow.hooks.classify import Intent, classify, write_key
from openadapt_flow.hooks.systems import SystemsError, SystemsFile, load_systems
from openadapt_flow.private_file import open_binary

REPORT_SCHEMA = "openadapt.hook-report/v1"
_RUN_OUTCOME = {
    "VERIFIED": "VERIFIED",
    "REFUTED": "FAILED",
    "UNCERTAIN": "RECONCILIATION_REQUIRED",
    "BLOCKED": "HALTED",
}
_LABEL = {"CONFIRMED": "VERIFIED", "REFUTED": "REFUTED", "UNCERTAIN": "UNCERTAIN"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_json(path: Path, payload: Any, *, private: bool = True) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    data = (json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n").encode()
    # A raw-byte descriptor (no Windows newline translation), every byte written.
    fd = open_binary(
        tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o644
    )
    try:
        _write_all(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


class Engine:
    """The hooks for one project folder."""

    def __init__(self, project: Path) -> None:
        self.root = Path(project).resolve()
        self.dir = self.root / HOOKS_DIR
        self.state = self.dir / "state"
        self.runs = self.dir / "runs"
        # The verifier records each snapshot it issues; keep that record in
        # the project, not the home folder.
        os.environ.setdefault(
            "OPENADAPT_VERIFIER_STATE_DIR", str(self.state / "verifier")
        )

    # -- state ---------------------------------------------------------------

    def _sub(self, name: str) -> Path:
        _private_dir(self.dir)
        _private_dir(self.state)
        return _private_dir(self.state / name)

    def _pending_key(self, inp: HookInput, digest: str) -> str:
        return _sha([inp.agent, inp.session_id, inp.tool_use_id or digest])[:32]

    def _log(self, entry: Mapping[str, Any]) -> None:
        self._sub("")
        line = json.dumps({"at": _now(), **entry}, sort_keys=True, default=str)
        fd = open_binary(
            self.state / "log.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        try:
            _write_all(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)

    def _held(self, digest: str) -> Optional[dict[str, Any]]:
        folder = self.state / "held"
        if not folder.is_dir():
            return None
        for path in sorted(folder.glob("*.json")):
            entry = _read_json(path)
            if entry and entry.get("hold_sha256") == digest:
                return entry
        return None

    def _granted(self, digest: str) -> Optional[Path]:
        folder = self.state / "approvals"
        if not folder.is_dir():
            return None
        for path in sorted(folder.glob("*.json")):
            entry = _read_json(path)
            if (
                entry
                and entry.get("hold_sha256") == digest
                and entry.get("status") == "granted"
            ):
                return path
        return None

    def _request(self, inp: HookInput, intent: Intent, digest: str) -> str:
        folder = self._sub("approvals")
        for path in sorted(folder.glob("*.json")):
            entry = _read_json(path)
            if entry and entry.get("hold_sha256") == digest:
                return path.stem
        request = "ap-" + secrets.token_hex(6)
        _write_json(
            folder / f"{request}.json",
            {
                "id": request,
                "status": "requested",
                "hold_sha256": digest,
                "agent": inp.agent,
                "system": intent.system.name if intent.system else None,
                "write": intent.write.name if intent.write else None,
                "detail": intent.detail,
                "requested_at": _now(),
            },
        )
        return request

    # -- events --------------------------------------------------------------

    def handle(self, inp: HookInput) -> HookOutput:
        if inp.event == "PreToolUse":
            return self.pre(inp)
        if inp.event in ("PostToolUse", "PostToolUseFailure"):
            return self.post(inp)
        if inp.event == "Stop":
            return self.stop(inp)
        return HookOutput()

    def _systems(self) -> SystemsFile:
        return load_systems(self.root / SYSTEMS_FILE)

    def pre(self, inp: HookInput) -> HookOutput:
        try:
            systems = self._systems()
        except SystemsError as exc:
            return HookOutput(
                "deny",
                f"OpenAdapt can't read {SYSTEMS_FILE}, so it can't tell which "
                f"calls write to a system of record: {exc}",
            )
        intent = classify(inp.tool_name, inp.tool_input, systems, self.root)
        digest = write_key(intent, inp.tool_name, inp.tool_input)
        base = {
            "agent": inp.agent,
            "session_sha256": _sha(inp.session_id),
            "event": inp.event,
            "tool": inp.tool_name,
            "kind": intent.kind,
            "system": intent.system.name if intent.system else None,
            "write": intent.write.name if intent.write else None,
            "hold_sha256": digest,
        }
        if intent.kind in ("none", "read"):
            if intent.kind == "read":
                self._log({**base, "decision": "pass"})
            return HookOutput()
        if intent.kind == "protected":
            self._log({**base, "decision": "deny"})
            return HookOutput(
                "deny",
                "OpenAdapt blocked this call: it "
                + intent.detail
                + ". Only a person changes the declared systems, the approvals "
                "and the hook settings.",
            )
        name = intent.system.name if intent.system else "a declared system"
        held = self._held(digest)
        if held is not None:
            self._log({**base, "decision": "deny", "held": held.get("write_id")})
            return HookOutput(
                "deny",
                f"OpenAdapt blocked this call: the same write to {name} came out "
                f"{held.get('label')} ({held.get('write_id')}). Don't retry it: "
                "the write may already be there. Ask the person to check the "
                f"record and release it with `openadapt hooks approve "
                f"{held.get('write_id')}`.",
            )
        mode: Literal["allow", "ask", "block"]
        if intent.kind == "unknown_write":
            mode = systems.unknown_write
            why = (
                f"{intent.detail}. {name} is a declared system of record and "
                f"{SYSTEMS_FILE} declares no such write"
            )
        else:
            assert intent.write is not None
            mode = intent.write.approval
            why = f"write {intent.write.name} to {name}"
        if mode == "block":
            run = self._finish(inp, intent, digest, "BLOCKED", [f"blocked: {why}"])
            self._log({**base, "decision": "deny", "write_id": run})
            return HookOutput(
                "deny",
                f"OpenAdapt blocked this call ({why}). Nothing was sent. Ask the "
                f"person to make this change or to declare it. Receipt: {run}.",
            )
        if mode == "ask" and inp.agent == "codex":
            granted = self._granted(digest)
            if granted is None:
                request = self._request(inp, intent, digest)
                self._log({**base, "decision": "deny", "approval": request})
                return HookOutput(
                    "deny",
                    f"OpenAdapt holds this call for a person's approval ({why}). "
                    f"Ask the person to run `openadapt hooks approve {request}` "
                    "in their own terminal, then make the same call again.",
                )
            granted.unlink(missing_ok=True)
            mode = "allow"  # a person approved this one call
        refusal = self._begin(inp, intent, digest)
        if refusal is not None:
            run = self._finish(inp, intent, digest, "BLOCKED", [refusal])
            self._log({**base, "decision": "deny", "write_id": run})
            return HookOutput(
                "deny", f"OpenAdapt blocked this call: {refusal}. Receipt: {run}."
            )
        self._log({**base, "decision": mode})
        if mode == "ask":
            return HookOutput(
                "ask",
                f"OpenAdapt: {why}. It will be read back and get a receipt.",
            )
        if intent.write is not None and intent.write.auto_approve:
            return HookOutput("allow", f"OpenAdapt: {why} is declared and in scope.")
        return HookOutput()

    def _begin(self, inp: HookInput, intent: Intent, digest: str) -> Optional[str]:
        """Read the store before the call. A reason string means don't send."""
        write_id = "hk-" + secrets.token_hex(8)
        pending: dict[str, Any] = {
            "write_id": write_id,
            "agent": inp.agent,
            "session_sha256": _sha(inp.session_id),
            "tool_use_id": inp.tool_use_id,
            "hold_sha256": digest,
            "system": intent.system.name if intent.system else None,
            "write": intent.write.name if intent.write else None,
            "detail": intent.detail,
            "started_at": _now(),
        }
        if intent.write is not None:
            from openadapt_flow.verifier import (
                BaselineUnreadable,
                Contract,
                VerifierError,
                capture,
            )

            assert intent.system is not None
            path = self._systems_contract(intent)
            try:
                contract = Contract.load(path)
            except (VerifierError, OSError) as exc:
                return f"the contract for write {intent.write.name} can't be loaded ({type(exc).__name__})"
            missing = [p for p in contract.params if p not in intent.params]
            if missing:
                return (
                    f"the call doesn't name the values contract "
                    f"{intent.write.name} needs: {', '.join(missing)}"
                )
            params = {p: intent.params[p] for p in contract.params}
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    snapshot = capture(contract, params)
            except BaselineUnreadable as exc:
                return (
                    f"{intent.system.name} can't be read before the write, so "
                    f"nothing could check it afterwards ({exc.reason})"
                )
            except VerifierError as exc:
                return f"the read before the write failed ({type(exc).__name__})"
            snap = self._sub("snapshots") / f"{write_id}.json"
            snapshot.save(snap)
            pending.update(
                {"contract": str(path), "snapshot": str(snap), "params": params}
            )
        _write_json(
            self._sub("pending") / f"{self._pending_key(inp, digest)}.json", pending
        )
        return None

    def _systems_contract(self, intent: Intent) -> Path:
        systems = self._systems()
        assert intent.write is not None
        return systems.contract_path(intent.write)

    def _claim(self, path: Path) -> Optional[dict[str, Any]]:
        claimed = path.with_name(path.name + f".{os.getpid()}.claimed")
        try:
            os.replace(path, claimed)
        except OSError:
            return None
        entry = _read_json(claimed)
        claimed.unlink(missing_ok=True)
        return entry

    def post(self, inp: HookInput) -> HookOutput:
        try:
            intent = classify(inp.tool_name, inp.tool_input, self._systems(), self.root)
        except SystemsError:
            intent = None
        digest = (
            write_key(intent, inp.tool_name, inp.tool_input)
            if intent is not None
            else ""
        )
        path = self.state / "pending" / f"{self._pending_key(inp, digest)}.json"
        pending = self._claim(path) if path.is_file() else None
        if pending is None:
            if intent is None:
                return HookOutput()
            if not intent.touches_system:
                return HookOutput()
            run = self._finish(
                inp,
                intent,
                digest,
                "UNCERTAIN",
                ["no pre-check ran for this call, so nothing was read before it"],
            )
            return self._tell(
                intent.system.name if intent.system else "", "", "UNCERTAIN", run, []
            )
        label, reasons, result = self._read_back(pending)
        if inp.event == "PostToolUseFailure":
            reasons = ["the tool call reported a failure", *reasons]
        run = self._settle(pending, label, reasons, result)
        return self._tell(
            pending.get("system") or "", pending.get("write") or "", label, run, reasons
        )

    def _read_back(self, pending: Mapping[str, Any]) -> tuple[str, list[str], Any]:
        if not pending.get("contract"):
            return (
                "UNCERTAIN",
                ["no contract declares this write, so it was not read back"],
                None,
            )
        from openadapt_flow.verifier import Actor, Contract, Snapshot, verify_write

        try:
            contract = Contract.load(pending["contract"])
            snapshot = Snapshot.load(pending["snapshot"])
            actor = Actor(
                "agent", f"{pending['agent']}-{pending['session_sha256'][:12]}"
            )
            result = verify_write(
                contract,
                snapshot,
                params=pending["params"],
                actor=actor,
                write_attempted=True,
                authorization_ref=pending["write_id"],
            )
        except Exception as exc:  # noqa: BLE001 - any failure is UNCERTAIN, never a pass
            return (
                "UNCERTAIN",
                [f"the read-back failed ({type(exc).__name__})"],
                None,
            )
        finally:
            Path(str(pending.get("snapshot"))).unlink(missing_ok=True)
        return _LABEL[result.outcome.value], list(result.reasons), result

    def _settle(
        self,
        pending: Mapping[str, Any],
        label: str,
        reasons: list[str],
        result: Any,
    ) -> str:
        run = self._run_folder(
            pending["write_id"],
            agent=pending["agent"],
            system=pending.get("system"),
            write=pending.get("write"),
            label=label,
            reasons=reasons,
            digest=pending["hold_sha256"],
            started=pending.get("started_at") or _now(),
            result=result,
            contract=pending.get("contract"),
        )
        if label != "VERIFIED":
            _write_json(
                self._sub("held") / f"{pending['write_id']}.json",
                {
                    "write_id": pending["write_id"],
                    "label": label,
                    "hold_sha256": pending["hold_sha256"],
                    "system": pending.get("system"),
                    "write": pending.get("write"),
                    "held_at": _now(),
                },
            )
        self._log(
            {
                "agent": pending["agent"],
                "session_sha256": pending.get("session_sha256"),
                "event": "read_back",
                "system": pending.get("system"),
                "write": pending.get("write"),
                "label": label,
                "write_id": pending["write_id"],
                "hold_sha256": pending["hold_sha256"],
            }
        )
        return run

    def _finish(
        self,
        inp: HookInput,
        intent: Intent,
        digest: str,
        label: str,
        reasons: list[str],
    ) -> str:
        """A receipt for a call that won't be read back (blocked, or no baseline)."""
        write_id = "hk-" + secrets.token_hex(8)
        return self._run_folder(
            write_id,
            agent=inp.agent,
            system=intent.system.name if intent.system else None,
            write=intent.write.name if intent.write else None,
            label=label,
            reasons=reasons,
            digest=digest,
            started=_now(),
            result=None,
            contract=None,
        )

    def _run_folder(
        self,
        write_id: str,
        *,
        agent: str,
        system: Optional[str],
        write: Optional[str],
        label: str,
        reasons: list[str],
        digest: str,
        started: str,
        result: Any,
        contract: Optional[str],
    ) -> str:
        _private_dir(self.dir)
        run_dir = _private_dir(_private_dir(self.runs) / write_id)
        effect: Optional[bool]
        effect = {"VERIFIED": True, "REFUTED": False}.get(label)
        sent = label != "BLOCKED"
        name = ".".join(x for x in (system, write or "undeclared") if x)
        contract_hash = f"sha256:{_sha(contract or 'undeclared')}"
        evidence: list[dict[str, Any]] = []
        if sent and effect is not None:
            evidence.append(
                {
                    "effect_contract_hash": contract_hash,
                    "substrate": "api",
                    "initial_verdict": "confirmed" if effect else "refuted",
                    "final_verdict": "confirmed" if effect else "refuted",
                    "observed_effect": "present" if effect else "absent",
                    "verification_tier": 1,
                }
            )
        report: dict[str, Any] = {
            "schema_version": REPORT_SCHEMA,
            "workflow_name": name[:48],
            "run_id": write_id,
            "started_at": started,
            "finished_at": _now(),
            "execution_outcome": _RUN_OUTCOME[label],
            "outcome": label,
            "reasons": reasons,
            # OpenAdapt's check made no model calls; the agent's are its own.
            "model_calls": 0,
            "executor": {"kind": "agent", "agent": agent},
            "system": system,
            "write": write,
            "hold_sha256": digest,
            "change_ledger": [],
            "results": [
                {
                    "step_id": write_id,
                    "intent": f"hook {name}",
                    "ok": label == "VERIFIED",
                    "actuation": "api",
                    **(
                        {
                            "effect_verified": effect,
                            # An undeclared write has no contract: its line
                            # says effect_not_declared. A declared write whose
                            # read-back didn't settle says approved_unverified
                            # (record/run/v1 has no read-back-failed code).
                            "effect_contract_hashes": [contract_hash],
                            "effect_evidence": evidence,
                            "effect_approved_unverified": bool(contract)
                            and effect is None,
                        }
                        if sent
                        else {}
                    ),
                    **(
                        {
                            "effect_field_checks": [
                                {
                                    "field": "delta",
                                    "matched": True,
                                    "verification_tier": 1,
                                }
                            ]
                        }
                        if result is not None
                        and (result.checked or {}).get("delta") == "held"
                        else {}
                    ),
                }
            ],
            "outcome_envelope": {
                "required_contracts": {"effect": 1 if sent else 0, "authorization": 1},
                "passed_contracts": {
                    "effect": 1 if label == "VERIFIED" else 0,
                    "authorization": 1 if sent else 0,
                },
            },
        }
        if result is not None:
            report["checked"] = {
                k: v for k, v in (result.checked or {}).items() if k != "coverage"
            }
            report["verifier_outcome"] = result.outcome.value
            report["effect_receipt_sha256"] = result.receipt_sha256
            _write_json(run_dir / "effect-receipt.json", result.receipt)
        _write_json(run_dir / "report.json", report, private=False)
        receipt = "receipt.json"
        try:
            from openadapt_flow.offline_seal import seal_home, signer_for
            from openadapt_flow.receipt_record import write_receipt_record

            write_receipt_record(run_dir, signer=signer_for(seal_home()))
        except Exception as exc:  # noqa: BLE001 - the outcome stands without it
            receipt = f"not signed ({type(exc).__name__})"
            _write_json(run_dir / "receipt.error.json", {"error": str(exc)})
        rel = run_dir.relative_to(self.root)
        return f"{rel}/{receipt}" if receipt == "receipt.json" else f"{rel} ({receipt})"

    def _tell(
        self, system: str, write: str, label: str, run: str, reasons: list[str]
    ) -> HookOutput:
        what = f"write {write} to {system}" if write else f"this write to {system}"
        if label == "VERIFIED":
            return HookOutput(
                context=f"OpenAdapt read the record back: {what} is VERIFIED. "
                f"Receipt: {run}."
            )
        why = "; ".join(reasons) or "no reason given"
        return HookOutput(
            context=f"OpenAdapt: {what} is {label}, not verified ({why}). Don't "
            "retry it: it may already be there. Tell the person, who checks the "
            f"record. Receipt: {run}.",
            flag=True,
        )

    def stop(self, inp: HookInput) -> HookOutput:
        """Read back writes whose PostToolUse never came; summarize the session."""
        session = _sha(inp.session_id)
        folder = self.state / "pending"
        if folder.is_dir():
            for path in sorted(folder.glob("*.json")):
                entry = _read_json(path)
                if not entry or entry.get("session_sha256") != session:
                    continue
                pending = self._claim(path)
                if pending is None:
                    continue
                label, reasons, result = self._read_back(pending)
                reasons = [
                    "no PostToolUse came for this call (declined, interrupted "
                    "or failed); read back at Stop",
                    *reasons,
                ]
                self._settle(pending, label, reasons, result)
        counts: dict[str, int] = {}
        log = self.state / "log.jsonl"
        if log.is_file():
            for line in log.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("session_sha256") != session:
                    continue
                if entry.get("event") == "read_back":
                    counts[entry["label"]] = counts.get(entry["label"], 0) + 1
                elif entry.get("decision") == "deny":
                    counts["BLOCKED"] = counts.get("BLOCKED", 0) + 1
        if not counts or set(counts) == {"VERIFIED"}:
            return HookOutput()
        order = ("VERIFIED", "REFUTED", "UNCERTAIN", "BLOCKED")
        parts = [f"{counts[k]} {k}" for k in order if counts.get(k)]
        return HookOutput(
            message="OpenAdapt checked this session's writes to declared systems: "
            + ", ".join(parts)
            + f". Receipts are in {HOOKS_DIR}/runs; `openadapt hooks status` lists them."
        )


def approve(project: Path, item: str, *, deny: bool = False) -> str:
    """A person approves a held call (Codex ``ask``) or releases a held write."""
    engine = Engine(project)
    approval = engine.state / "approvals" / f"{item}.json"
    held = engine.state / "held" / f"{item}.json"
    if approval.is_file():
        entry = _read_json(approval) or {}
        if deny:
            approval.unlink()
            return f"{item}: declined"
        entry["status"] = "granted"
        entry["granted_at"] = _now()
        _write_json(approval, entry)
        return f"{item}: approved once; the agent can make the same call again"
    if held.is_file():
        held.unlink()
        return f"{item}: released; the same call is no longer refused"
    raise FileNotFoundError(f"no pending approval or held write named {item}")


def summary(project: Path, *, limit: int = 20) -> dict[str, Any]:
    """Pending approvals, held writes and recent receipts, for ``status``."""
    engine = Engine(project)

    def entries(name: str) -> list[dict[str, Any]]:
        folder = engine.state / name
        if not folder.is_dir():
            return []
        found = [_read_json(p) for p in sorted(folder.glob("*.json"))]
        return [e for e in found if e]

    runs = []
    if engine.runs.is_dir():
        folders = sorted(
            (p for p in engine.runs.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for run in folders[:limit]:
            report = _read_json(run / "report.json") or {}
            runs.append(
                {
                    "id": run.name,
                    "outcome": report.get("outcome"),
                    "system": report.get("system"),
                    "write": report.get("write"),
                    "receipt": (run / "receipt.json").is_file(),
                }
            )
    approvals = [e for e in entries("approvals") if e.get("status") == "requested"]
    return {
        "approvals": approvals,
        "held": entries("held"),
        "receipts": runs,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
