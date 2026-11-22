"""Plan, apply, undo and release a batch of writes to external systems.

``make_plan`` reads every record the batch touches (the baseline) and the
frame around it, predicts the diff with the system's own staged transaction
or a computed shadow, and gives the plan a digest. A person approves that
digest once. ``apply_plan`` refuses a plan whose frame moved since, consumes
the approval, then makes each write under its idempotency key and reads it
back. ``undo_plan`` compensates each applied write, latest first, and proves
the undo by reading every record and the whole frame back to the baseline.
Irreversible writes (email, a delete the API can't reverse) wait in a
cancelable holdback queue until ``release``.

Each phase writes ``report.json`` (digests and counts, never record values)
and a signed ``record/run/v1`` receipt. Record values live only in the
owner-only state folder.
"""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from openadapt_flow.txplan.adapters import (
    UNCONDITIONAL,
    AdapterError,
    ChangedSinceApply,
    Diverged,
    MailAdapter,
    RestAdapter,
    SqlAdapter,
    StaleBaseline,
    adapter_for,
    after_image,
    column_digests,
    diff_frames,
    same,
    volatile_columns,
)
from openadapt_flow.txplan.spec import (
    Collection,
    MailSystem,
    RestSystem,
    SqlSystem,
    TxSpec,
)
from openadapt_flow.txplan.state import (
    State,
    StateError,
    iso,
    now,
    read_json,
    sha256,
    write_private,
)

PLAN_SCHEMA = "openadapt.tx-plan/v1"
REPORT_SCHEMA = "openadapt.tx-report/v1"
OPS = ("create", "update", "delete", "send", "sql")
#: Ledger states after which a key is free again.
FREE = ("undone", "cancelled", "refused")
_DDL = re.compile(
    r"^\s*(create|alter|drop|truncate|rename|comment|grant|revoke)\b", re.I
)
_DML = re.compile(r"^\s*(insert|update|delete|merge|replace)\b", re.I)
_READ = re.compile(r"^\s*(select|with|show|explain|pragma|values)\b", re.I)
#: Test hook: exit right after the Nth send, before its outcome is recorded.
CRASH_ENV = "OPENADAPT_TX_CRASH_AFTER_SEND"
_TX_CONTROL = re.compile(
    r"^\s*(BEGIN|COMMIT|END|ROLLBACK|ABORT|SAVEPOINT|RELEASE|START\s+TRANSACTION|"
    r"SET\s+(SESSION\s+)?TRANSACTION|PREPARE\s+TRANSACTION|ATTACH|DETACH|PRAGMA|"
    r"VACUUM|LOCK|UNLOCK|XA)\b",
    re.IGNORECASE,
)


class TxRefused(RuntimeError):
    def __init__(
        self, code: str, detail: str = "", report: Optional[dict] = None
    ) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.report = report


# -- helpers -------------------------------------------------------------------


def _scope(write: Mapping[str, Any], system: Any) -> str:
    """The frame a write belongs to: a REST collection, a whole database, a mailbox."""
    if isinstance(system, RestSystem):
        return f"{system.name}:{write.get('collection')}"
    return f"{system.name}:*"


def _coll(system: Any, write: Mapping[str, Any]) -> Optional[Collection]:
    return (
        system.collection(write.get("collection"))
        if isinstance(system, RestSystem)
        else None
    )


def _irreversible(system: Any, write: Mapping[str, Any]) -> bool:
    if isinstance(system, MailSystem):
        return True
    coll = _coll(system, write)
    return bool(
        coll is not None and write["op"] == "delete" and not coll.delete_reversible
    )


def _norm(
    system: Any, write: Mapping[str, Any], record: Optional[Mapping[str, Any]]
) -> Optional[dict]:
    if record is None:
        return None
    coll = _coll(system, write)
    drop = list(coll.server_fields) if coll is not None else []
    if isinstance(system, SqlSystem):
        table = str(write.get("collection"))
        drop += [
            r.split(".", 1)[1]
            for r in system.frame_ignore_columns
            if r.startswith(f"{table}.") or r.startswith("*.")
        ]
    return column_digests(record, drop)


def _empty_prediction() -> dict[str, Any]:
    return {"added": {}, "changed": {}, "removed": [], "derived": {}}


def _merge(into: dict[str, Any], more: Mapping[str, Any]) -> None:
    for coll, n in more.get("added", {}).items():
        into["added"][coll] = into["added"].get(coll, 0) + n
    for rid, cols in more.get("changed", {}).items():
        into["changed"][rid] = sorted(set(into["changed"].get(rid, [])) | set(cols))
    for rid, cols in more.get("derived", {}).items():
        into["derived"][rid] = sorted(set(into["derived"].get(rid, [])) | set(cols))
    into["removed"] = sorted(set(into["removed"]) | set(more.get("removed", [])))


def _staged_to_prediction(diff: Mapping[str, Any]) -> dict[str, Any]:
    added: dict[str, int] = {}
    for rid in diff["added"]:
        table = rid.split("/", 1)[0]
        added[table] = added.get(table, 0) + 1
    return {
        "added": added,
        "changed": dict(diff["changed"]),
        "removed": list(diff["removed"]),
        "derived": {},
    }


def compare(predicted: Mapping[str, Any], actual: Mapping[str, Any]) -> dict[str, Any]:
    """Missed effects (the read-back found what the dry run didn't predict) and
    false diffs (the dry run predicted what the read-back didn't find), each
    counted per record."""
    missed: list[str] = []
    false: list[str] = []
    got_added: dict[str, int] = {}
    for rid in actual["added"]:
        coll = rid.split("/", 1)[0]
        got_added[coll] = got_added.get(coll, 0) + 1
    for coll in set(got_added) | set(predicted["added"]):
        extra = got_added.get(coll, 0) - predicted["added"].get(coll, 0)
        missed += [f"{coll}/+new"] * max(0, extra)
        false += [f"{coll}/+new"] * max(0, -extra)
    for rid, cols in actual["changed"].items():
        allowed = set(predicted["changed"].get(rid, [])) | set(
            predicted["derived"].get(rid, [])
        )
        if set(cols) - allowed:
            missed.append(rid)
    for rid, cols in predicted["changed"].items():
        if set(cols) - set(actual["changed"].get(rid, [])):
            false.append(rid)
    missed += [r for r in actual["removed"] if r not in predicted["removed"]]
    false += [r for r in predicted["removed"] if r not in actual["removed"]]
    return {"missed": sorted(missed), "false": sorted(false)}


@dataclass
class Ctx:
    spec: TxSpec
    state: State
    adapters: dict[str, Any] = field(default_factory=dict)

    def adapter(self, name: str) -> Any:
        if name not in self.adapters:
            self.adapters[name] = adapter_for(self.spec.system(name), self.spec.root)
        return self.adapters[name]

    def frames(self, scopes: Sequence[str]) -> dict[str, dict[str, dict[str, str]]]:
        out = {}
        for scope in sorted(set(scopes)):
            name, coll = scope.split(":", 1)
            out[scope] = self.adapter(name).frame(None if coll == "*" else coll)
        return out


def _spec_sha(spec: TxSpec) -> str:
    # Wrong-target class: a relative SQLite path names a file relative to the
    # tx file (or the working folder), so the approved digest binds the file
    # it resolved to, not just the spelling.
    resolved = {}
    for system in spec.systems:
        path = getattr(system, "path", None)
        if getattr(system, "dialect", None) == "sqlite" and path:
            p = Path(str(path))
            resolved[system.name] = str(
                (p if p.is_absolute() else spec.root / p).resolve()
            )
    return sha256({"spec": spec.model_dump(mode="json"), "sqlite_files": resolved})


# -- receipts ------------------------------------------------------------------


def _approval_block(
    approval: Optional[Mapping[str, Any]], digest: str
) -> dict[str, Any]:
    block: dict[str, Any] = {
        "schema": "openadapt.approval-mode-item/v1",
        "mode": "each_item",
        "approvals": [],
    }
    if approval:
        block["decision"] = "approved"
        block["approvals"].append(
            {
                "decision": "approved",
                "decided_by": approval.get("decided_by", "human"),
                "approver_id": approval.get("approver_id"),
                "request_sha256": digest,
            }
        )
    return block


def _write_run(
    state: State,
    digest: str,
    phase: str,
    *,
    outcome: str,
    results: list[dict[str, Any]],
    tx: Mapping[str, Any],
    approval: Optional[Mapping[str, Any]],
    started: float,
    sign: bool = True,
) -> Path:
    run_id = f"tx-{phase}-{digest[:12]}-{secrets.token_hex(3)}"
    run_dir = state.root / "runs" / run_id
    run_dir.mkdir(parents=True, mode=0o700)
    writes = [r for r in results if r.get("effect_contract_hashes")]
    confirmed = sum(1 for r in writes if r.get("effect_verified") is True)
    report = {
        "schema_version": REPORT_SCHEMA,
        "workflow_name": f"tx-{phase}",
        "run_id": run_id,
        "started_at": iso(started),
        "finished_at": iso(now()),
        "plan_sha256": digest,
        "execution_outcome": outcome,
        "success": outcome in ("VERIFIED", "ROLLED_BACK"),
        "model_calls": 0,
        "change_ledger": [],
        "results": results,
        "approval": _approval_block(approval, digest),
        "outcome_envelope": {
            "required_contracts": {
                "effect": len(writes),
                "authorization": 1 if approval else 0,
            },
            "passed_contracts": {
                "effect": confirmed,
                "authorization": 1 if approval else 0,
            },
        },
        "tx": dict(tx),
    }
    write_private(run_dir / "report.json", report)
    os.chmod(run_dir / "report.json", 0o644)
    if sign:
        try:
            from openadapt_flow.offline_seal import seal_home, signer_for
            from openadapt_flow.receipt_record import write_receipt_record

            write_receipt_record(run_dir, signer=signer_for(seal_home()))
        except Exception as exc:  # noqa: BLE001 - the outcome stands without it
            write_private(
                run_dir / "receipt.error.json",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
    return run_dir


def receipt_status(run_dir: Path | str) -> str:
    """``signed``, or why the phase has no receipt."""
    run_dir = Path(run_dir)
    if (run_dir / "receipt.json").exists():
        return "signed"
    err = run_dir / "receipt.error.json"
    return (
        f"not signed: {read_json(err).get('error', '')[:120]}"
        if err.exists()
        else "not signed"
    )


def _line(
    *, ok: bool, verified: Optional[bool], irreversible: bool, payload: str, code: str
) -> dict[str, Any]:
    contract_hash = payload if payload.startswith("sha256:") else f"sha256:{payload}"
    evidence: list[dict[str, Any]] = []
    if verified is not None:
        evidence.append(
            {
                "effect_contract_hash": contract_hash,
                "substrate": "api",
                "initial_verdict": "confirmed" if verified else "refuted",
                "final_verdict": "confirmed" if verified else "refuted",
                "observed_effect": "present" if verified else "absent",
                "verification_tier": 1,
            }
        )
    return {
        "step_id": f"tx-{code}",
        "intent": f"tx {code}",
        "ok": ok,
        "effect_verified": verified,
        "effect_approved_unverified": verified is None,
        "actuation": "api",
        "risk": "irreversible" if irreversible else "reversible",
        "effect_contract_hashes": [contract_hash],
        "effect_evidence": evidence,
        "effect_field_checks": (
            [{"field": "delta", "matched": verified, "verification_tier": 1}]
            if verified is not None
            else []
        ),
        "postconditions_ok": None,
        "tx_code": code,
    }


# -- plan ----------------------------------------------------------------------


def _counts(diff: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """A staged diff as counts per table (what plan.json may hold)."""
    if not diff:
        return None
    out: dict[str, dict[str, int]] = {}
    for what in ("added", "removed"):
        for rid in diff[what]:
            t = out.setdefault(
                rid.split("/", 1)[0], {"added": 0, "changed": 0, "removed": 0}
            )
            t[what] += 1
    for rid in diff["changed"]:
        t = out.setdefault(
            rid.split("/", 1)[0], {"added": 0, "changed": 0, "removed": 0}
        )
        t["changed"] += 1
    return out


def _row_anomalies(
    spec: TxSpec,
    writes: Sequence[Mapping[str, Any]],
    rows: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Blast-radius and row-value rules, from the staged transaction's rows."""
    found = []
    touched = sum(
        len(d["added"]) + len(d["removed"]) + len(d["changed"])
        for w in writes
        if (d := w.get("staged_diff"))
    )
    for rule in spec.anomalies:
        if rule.max_rows is not None and touched > rule.max_rows:
            found.append(
                {"label": rule.label, "write": None, "detail": "rows", "value": touched}
            )
        if rule.table and rule.column:
            hits = sum(
                1
                for n, before in rows.items()
                for rid, row in before.items()
                if rid.split("/", 1)[0] == rule.table
                and same(row.get(rule.column), rule.equals)
            )
            if hits:
                found.append(
                    {
                        "label": rule.label,
                        "write": None,
                        "detail": f"{rule.table}.{rule.column}",
                        "value": hits,
                    }
                )
    for n, w in enumerate(writes):
        if w.get("ddl"):
            found.append(
                {
                    "label": "schema change: no generated undo",
                    "write": n,
                    "detail": "ddl",
                }
            )
        if w.get("volatile_other"):
            found.append(
                {
                    "label": "values differ between dry runs (not compared at apply)",
                    "write": n,
                    "detail": "volatile",
                    "value": ", ".join(w["volatile_other"]),
                }
            )
    return found


def _row_digests(adapter: Any, rid: str, row: Mapping[str, Any]) -> dict[str, str]:
    from openadapt_flow.txplan.adapters import digest as _d

    table = rid.split("/", 1)[0]
    return {c: _d(v) for c, v in row.items() if not adapter._ignored(table, c)}


def _undo_sql(
    state: State,
    adapter: Any,
    w: Mapping[str, Any],
    entry: Any,
    images: Any,
    digest: str,
) -> tuple[str, list[str]]:
    if w.get("ddl") or not images:
        return "no_undo", []
    rids = [*images.get("after", {}), *images.get("before", {})]
    current = adapter.current_digests(rids)
    for rid, want in images.get("after", {}).items():
        if current.get(rid) != want:
            return "changed_since_apply", []
    for rid in images.get("before", {}):
        if rid not in images.get("after", {}) and current.get(rid) is not None:
            return "changed_since_apply", []  # a removed row came back
    state.record(w["key"], "undo_begun", plan=digest, payload=entry.payload)
    try:
        gaps = adapter.compensate_sql(images)
    except ChangedSinceApply:
        return "changed_since_apply", []
    except AdapterError as exc:
        state.record(
            w["key"],
            "uncertain",
            plan=digest,
            payload=entry.payload,
            reason=f"undo: {exc}"[:120],
        )
        return "undo_failed", []
    now_ = adapter.current_digests(rids + list(images.get("added_pk", {})))
    ok = all(now_.get(r) is None for r in images.get("added_pk", {})) and all(
        now_.get(r) == _row_digests(adapter, r, item["row"])
        for r, item in images.get("before", {}).items()
    )
    if ok:
        state.record(
            w["key"], "undone", plan=digest, payload=entry.payload, read_back=ok
        )
    else:
        # Unverified-success class: a refuted SQL undo keeps its replay key.
        state.record(
            w["key"],
            "uncertain",
            plan=digest,
            payload=entry.payload,
            reason="undo: the read-back does not show the baseline",
        )
    return ("undone" if ok else "undo_refuted"), gaps


def _check_write(spec: TxSpec, n: int, w: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(w, Mapping):
        raise TxRefused("bad_write", f"write {n} is not an object")
    extra = set(w) - {"system", "collection", "op", "id", "fields", "label"}
    if extra:
        raise TxRefused("bad_write", f"write {n} has unknown keys {sorted(extra)}")
    system = spec.system(str(w.get("system")))
    op = w.get("op")
    if op not in OPS:
        raise TxRefused("bad_write", f"write {n}: op must be one of {OPS}")
    if op == "sql":
        if not isinstance(system, SqlSystem):
            raise TxRefused(
                "bad_write", f"write {n}: a SQL statement needs a sql system"
            )
        sql = (
            (w.get("fields") or {}).get("sql")
            if isinstance(w.get("fields"), Mapping)
            else None
        )
        if not isinstance(sql, str) or not sql.strip():
            raise TxRefused("bad_write", f"write {n}: sql needs fields.sql")
        sql = sql.strip().rstrip(";").strip()
        if ";" in sql:
            raise TxRefused("bad_write", f"write {n}: one statement per write")
        if _READ.match(sql):
            raise TxRefused(
                "bad_write", f"write {n}: a read is not a write (use a query)"
            )
        if not (_DML.match(sql) or _DDL.match(sql)):
            # Unstaged-commit class: only statements the staging transaction
            # can roll back are admitted.
            raise TxRefused(
                "bad_write", f"write {n}: only INSERT, UPDATE, DELETE, MERGE or DDL"
            )
        if _TX_CONTROL.match(sql):
            # Unstaged-commit class: a transaction-control statement would end
            # the staging transaction and commit earlier writes before approval.
            raise TxRefused(
                "bad_write", f"write {n}: transaction control is not a write"
            )
        ddl = bool(_DDL.match(sql))
        if ddl and system.dialect == "mysql":
            raise TxRefused(
                "bad_write",
                f"write {n}: MySQL commits DDL at once, so it can't be staged",
            )
        return {
            "system": system.name,
            "collection": "statement",
            "op": "sql",
            "id": None,
            "fields": {"sql": sql},
            "label": str(w.get("label") or "")[:80],
            "ddl": ddl,
        }
    if isinstance(system, MailSystem) != (op == "send"):
        raise TxRefused(
            "bad_write", f"write {n}: {op} doesn't fit a {system.kind} system"
        )
    if isinstance(system, RestSystem):
        coll = system.collection(w.get("collection"))
        if getattr(coll, op) is None:
            raise TxRefused("bad_write", f"write {n}: {coll.name} declares no {op}")
    if isinstance(system, SqlSystem) and w.get("collection") not in system.tables:
        raise TxRefused(
            "bad_write", f"write {n}: table {w.get('collection')!r} is not declared"
        )
    if op in ("update", "delete") and w.get("id") in (None, ""):
        raise TxRefused("bad_write", f"write {n}: {op} needs id")
    if op in ("create", "send") and w.get("id") not in (None, ""):
        raise TxRefused(
            "bad_write", f"write {n}: {op} takes no id (the slot is its ordinal)"
        )
    if op != "delete" and not isinstance(w.get("fields"), Mapping):
        raise TxRefused("bad_write", f"write {n}: {op} needs fields")
    return {
        "system": system.name,
        "collection": w.get("collection") or ("outbox" if op == "send" else None),
        "op": op,
        "id": None if w.get("id") is None else str(w["id"]),
        "fields": dict(w.get("fields") or {}),
        "label": str(w.get("label") or "")[:80],
    }


def _anomalies(
    spec: TxSpec, writes: Sequence[Mapping[str, Any]], baselines: Sequence[Any]
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for rule in spec.anomalies:
        if rule.max_writes is None and not (rule.field or rule.op or rule.clears):
            continue  # a row rule (max_rows, table/column): see _row_anomalies
        if rule.max_writes is not None:
            if len(writes) <= rule.max_writes:
                continue
            found.append(
                {"label": rule.label, "write": None, "detail": f"{len(writes)} writes"}
            )
            continue
        for n, (w, base) in enumerate(zip(writes, baselines)):
            if rule.op is not None and w["op"] != rule.op:
                continue
            fields = w.get("fields") or {}
            if rule.field is not None:
                if rule.field not in fields:
                    continue
                value = fields[rule.field]
                if rule.above is not None:
                    try:
                        if not float(value) > rule.above:
                            continue
                    except (TypeError, ValueError):
                        continue
                if rule.clears and not (
                    (base or {}).get(rule.field) not in (None, "")
                    and value in (None, "")
                ):
                    continue
            elif rule.clears:
                if not any(
                    (base or {}).get(k) not in (None, "") and v in (None, "")
                    for k, v in fields.items()
                ):
                    continue
            found.append(
                {
                    "label": rule.label,
                    "write": n,
                    "detail": rule.field or w["op"],
                    "value": fields.get(rule.field) if rule.field else None,
                }
            )
    return found


def _slots(writes: Sequence[Mapping[str, Any]]) -> list[str]:
    seen: dict[tuple[str, str, str], int] = {}
    out = []
    for w in writes:
        if w["op"] in ("update", "delete"):
            out.append(f"id:{w['id']}")
            continue
        k = (w["system"], str(w["collection"]), w["op"])
        seen[k] = seen.get(k, 0) + 1
        out.append(f"{w['op']}#{seen[k]}")
    return out


def make_plan(ctx: Ctx, task: str, raw: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Read the baselines, predict the diff and return the plan (not yet approved)."""
    started = now()
    if not task or len(task) > 128:
        raise TxRefused("bad_task", "the task id must be 1-128 characters")
    spec, state = ctx.spec, ctx.state
    writes = [_check_write(spec, n, w) for n, w in enumerate(raw)]
    if not writes:
        raise TxRefused("empty_plan", "a plan needs at least one write")
    slots = _slots(writes)
    # A statement's record is the set of rows it changes or removes, read from
    # a first staged run: a regenerated UPDATE that touches the same rows gets
    # the same key whatever its values. A statement that only inserts has no
    # such identity; its slot is the table, so a second insert into it in the
    # same task is a possible replay that a person must approve.
    sql_idx: dict[str, list[int]] = {}
    for n, w in enumerate(writes):
        if w["op"] == "sql":
            sql_idx.setdefault(w["system"], []).append(n)
    for name, idx in sql_idx.items():
        _, pre = ctx.adapter(name).stage([writes[i] for i in idx], [None] * len(idx))
        for i, res in zip(idx, pre):
            d = res["staged_diff"]
            touched = sorted([*d["changed"], *d["removed"]])
            tables = sorted({r.split("/", 1)[0] for r in d["added"]})
            if touched:
                slots[i] = "rows:" + sha256(touched)[:32]
            else:
                slots[i] = "insert:" + ",".join(tables) if tables else "noop"
            writes[i]["row_tags"] = sorted(
                {state.tag(r) for r in touched}
                | {state.tag("insert:" + t) for t in tables}
            )
    for w, slot in zip(writes, slots):
        if w["op"] in ("update", "delete"):
            dup = [
                x
                for x in writes
                if x is not w
                and x["system"] == w["system"]
                and x["collection"] == w["collection"]
                and x["id"] == w["id"]
            ]
            if dup:
                raise TxRefused("bad_write", "a plan writes each record once")
        w["slot"] = slot
        w["key"] = state.key_for(
            task=task,
            system=w["system"],
            collection=str(w["collection"]),
            op=w["op"],
            slot=slot,
        )
        w["payload_sha256"] = sha256(
            {"op": w["op"], "fields": w["fields"], "id": w["id"]}
        )
        w["irreversible"] = _irreversible(spec.system(w["system"]), w)
    # The ledger first: a key already used under another payload is a replay.
    task_tag = state.tag("task:" + task)
    applied_rows: dict[str, set[str]] = {}
    if any(w["op"] == "sql" for w in writes):
        for e in state.entries():
            begun = next((x for x in reversed(e.events) if x["event"] == "begun"), {})
            if e.status not in FREE and begun.get("task_tag") == task_tag:
                applied_rows[e.key] = set(begun.get("rows") or [])
    replays = []
    for n, w in enumerate(writes):
        entry = state.entry(w["key"])
        w["ledger"] = "free"
        if entry is not None and entry.status not in FREE:
            w["ledger"] = (
                "replay" if entry.payload == w["payload_sha256"] else "replay_mismatch"
            )
            if w["ledger"] == "replay_mismatch" and w["op"] == "sql":
                # A raw statement's identity is the rows it touches, which new work
                # can share: a person decides, and policy never approves it.
                w["ledger"] = "possible_replay"
            replays.append(
                {"write": n, "status": w["ledger"], "prior_plan": entry.plan[:12]}
            )
        elif w["op"] == "sql":
            # Overlap, not only the exact row set: `id IN (9, 10)` after `id = 9`.
            mine = set(w.get("row_tags") or [])
            if any(k != w["key"] and mine & rows for k, rows in applied_rows.items()):
                w["ledger"] = "possible_replay"
                replays.append(
                    {"write": n, "status": "possible_replay", "prior_plan": ""}
                )
    baselines: list[Optional[dict]] = []
    for w in writes:
        base = None
        if w["op"] in ("update", "delete"):
            adapter = ctx.adapter(w["system"])
            if isinstance(adapter, RestAdapter):
                base, etag = adapter.read_versioned(w["collection"], w["id"])
                if base is not None:
                    # Stale-baseline class: the write carries the version
                    # read here, so the server refuses it if the record
                    # changed since; the plan digest binds it.
                    try:
                        cond = adapter.condition(w, base, etag)
                    except AdapterError as exc:
                        raise TxRefused("no_version", str(exc)) from exc
                    if cond:
                        w["if_match"] = cond
            else:
                base = adapter.read(w["collection"], w["id"])
            if base is None and w["ledger"] == "free":
                raise TxRefused(
                    "record_missing", f"{w['system']} {w['collection']} {w['id']}"
                )
        baselines.append(base)
    scopes = [_scope(w, spec.system(w["system"])) for w in writes]
    frames = ctx.frames(scopes)
    predicted: dict[str, dict[str, Any]] = {s: _empty_prediction() for s in set(scopes)}
    dry_run: dict[str, str] = {}
    queued = 0
    staged: dict[str, list[int]] = {}
    staged_rows: dict[int, dict[str, Any]] = {}
    for n, (w, base, scope) in enumerate(zip(writes, baselines, scopes)):
        adapter = ctx.adapter(w["system"])
        dry_run[w["system"]] = adapter.dry_run
        if w["ledger"] not in ("free", "possible_replay"):
            continue
        if isinstance(adapter, SqlAdapter):
            staged.setdefault(w["system"], []).append(n)
        elif w["irreversible"]:
            queued += 1
        else:
            _merge(predicted[scope], adapter.predict(w, base))
    for name, idx in staged.items():
        diff, results = ctx.adapter(name).stage(
            [writes[i] for i in idx], [baselines[i] for i in idx]
        )
        _merge(predicted[f"{name}:*"], _staged_to_prediction(diff))
        if any(writes[i]["op"] == "sql" for i in idx):
            # A second staged run finds the columns a statement sets differently
            # each time (now(), random(), a time default): apply doesn't compare them.
            _, again = ctx.adapter(name).stage(
                [writes[i] for i in idx], [baselines[i] for i in idx]
            )
            for i, res, res2 in zip(idx, results, again):
                if writes[i]["op"] == "sql":
                    vol = volatile_columns(res["after_raw"], res2["after_raw"])
                    writes[i]["volatile"] = vol
                    kinds = res["after_raw"].get("kinds", {})
                    writes[i]["volatile_other"] = [
                        c for c in vol if kinds.get(c) != "time"
                    ]
                    writes[i]["staged_after"] = after_image(res["after_raw"], vol)
        for i, res in zip(idx, results):
            # What the dry run could not close: the write will be at best
            # COMPLETED_UNVERIFIED, and the approver sees it before approving.
            writes[i]["staged_not_covered"] = list(res.get("not_covered") or [])
            if writes[i]["op"] == "sql":
                writes[i]["staged_diff"] = res["staged_diff"]
                writes[i]["staged_rows"] = res["rows"]
                staged_rows[i] = res.get("before_rows", {})
    summary: dict[str, int] = {}
    for w in writes:
        verb = {
            "create": "created",
            "update": "updated",
            "delete": "deleted",
            "send": "queued",
            "sql": "statements",
        }[w["op"]]
        if w["irreversible"] and w["op"] == "delete":
            verb = "deleted after holdback"
        k = (
            f"SQL statement(s) on {w['system']}"
            if w["op"] == "sql"
            else f"{w['system']}.{w['collection']} {verb}"
        )
        summary[k] = summary.get(k, 0) + 1
    anomalies = _anomalies(spec, writes, baselines)
    anomalies += _row_anomalies(spec, writes, staged_rows)
    for r in replays:
        if r["status"] == "possible_replay":
            anomalies.append(
                {
                    "label": "possible replay: this task already wrote these rows (or this table) with another statement",
                    "write": r["write"],
                    "detail": "possible_replay",
                }
            )
        if r["status"] == "replay_mismatch":
            anomalies.append(
                {
                    "label": "replays an applied write with a different payload",
                    "write": r["write"],
                    "detail": "replay_mismatch",
                }
            )
    for scope, pred in predicted.items():
        name = scope.split(":", 1)[0]
        if isinstance(spec.system(name), SqlSystem):
            declared = set(spec.system(name).tables)  # type: ignore[union-attr]
            other = (
                {rid.split("/", 1)[0] for rid in pred["changed"]}
                | set(pred["added"])
                | {rid.split("/", 1)[0] for rid in pred["removed"]}
            )
            if declared and other - declared:
                anomalies.append(
                    {
                        "label": "side effects in undeclared tables",
                        "write": None,
                        "detail": str(len(other - declared)),
                    }
                )
    core = {
        "schema_version": PLAN_SCHEMA,
        "task": task,
        "spec_sha256": _spec_sha(spec),
        "created_at": iso(started),
        "nonce": secrets.token_hex(8),
        "holdback_s": spec.holdback_s,
        "writes": [
            {
                **{
                    k: w[k]
                    for k in (
                        "system",
                        "collection",
                        "op",
                        "id",
                        "slot",
                        "key",
                        "payload_sha256",
                        "irreversible",
                        "ledger",
                    )
                },
                **(
                    {
                        "ddl": w["ddl"],
                        "staged_diff": _counts(w.get("staged_diff")),
                        "volatile": w.get("volatile", []),
                    }
                    if w["op"] == "sql"
                    else {}
                ),
                **(
                    {"not_covered": w["staged_not_covered"]}
                    if w.get("staged_not_covered")
                    else {}
                ),
            }
            for w in writes
        ],
        "baseline_sha256": [None if b is None else sha256(b) for b in baselines],
        "frame_sha256": {s: sha256(f) for s, f in frames.items()},
        "predicted": predicted,
        "queued": queued,
        "dry_run": dry_run,
        "summary": summary,
        "anomalies": [{k: a[k] for k in ("label", "write")} for a in anomalies],
    }
    private = {
        "writes": writes,
        "baselines": baselines,
        "frames": frames,
        "anomalies": anomalies,
    }
    # Unbound-evidence class: the approved digest covers the private plan
    # too, so editing private.json can't change what an approval authorizes.
    core["private_sha256"] = sha256(private)
    digest = sha256(core)
    pdir = state.plan_dir(digest)
    write_private(pdir / "plan.json", {**core, "digest": digest})
    write_private(pdir / "private.json", private)
    tx = {
        "phase": "plan",
        "writes": len(writes),
        "queued": queued,
        "replays": sum(1 for r in replays if r["status"] == "replay"),
        "replay_mismatches": sum(
            1 for r in replays if r["status"] == "replay_mismatch"
        ),
        "anomalies": len(anomalies),
        "dry_run": sorted(set(dry_run.values())),
    }
    run = _write_run(
        state,
        digest,
        "plan",
        outcome="COMPLETED_UNVERIFIED",
        results=[],
        tx=tx,
        approval=None,
        started=started,
    )
    return {
        **core,
        "digest": digest,
        "run_dir": str(run),
        "receipt": receipt_status(run),
        "anomaly_details": anomalies,
    }


def load_plan(state: State, digest: str) -> tuple[dict[str, Any], dict[str, Any]]:
    pdir = state.plan_dir(digest)
    if not (pdir / "plan.json").exists():
        raise TxRefused("plan_not_found", digest[:12])
    plan, private = read_json(pdir / "plan.json"), read_json(pdir / "private.json")
    core = {k: v for k, v in plan.items() if k != "digest"}
    if (
        plan.get("digest") != digest
        or sha256(core) != digest
        or plan.get("private_sha256") != sha256(private)
    ):
        raise TxRefused("plan_tampered", "the stored plan doesn't match its digest")
    return plan, private


def approve_plan(
    state: State, digest: str, *, approver: str, decided_by: str
) -> dict[str, Any]:
    load_plan(state, digest)
    return state.approve(digest, approver=approver, decided_by=decided_by)


# -- apply ---------------------------------------------------------------------


def _gaps(
    ctx: Ctx, w: Mapping[str, Any], result: Optional[Mapping[str, Any]]
) -> list[str]:
    """Why this write's read-back can't make it VERIFIED: what its adapter
    could not rule out (an unenforced precondition, an open write scope)."""
    fn = getattr(ctx.adapter(w["system"]), "not_covered", None)
    return list(fn(w, result)) if fn is not None else []


def _note_gaps(tx: dict[str, Any], gaps: Sequence[str]) -> None:
    tx["not_covered"] = sorted({*tx.get("not_covered", []), *gaps})
    tx["capped"] = tx.get("capped", 0) + 1
    if any(g in UNCONDITIONAL for g in gaps):
        tx["unconditional_writes"] = tx.get("unconditional_writes", 0) + 1


def _capped(
    ctx: Ctx,
    w: Mapping[str, Any],
    verified: Optional[bool],
    tx: dict[str, Any],
    result: Optional[Mapping[str, Any]],
) -> Optional[bool]:
    """Closed-scope class: a read-back confirms a write, but VERIFIES it only
    when nothing it can't see could have changed data around it. A write
    sent with no enforced version check may have erased another caller's
    edit and still read back like this write; a database write whose scope
    inventory is unreadable or not empty may have written outside the frame.
    Either one caps the write, on every path that can mark it verified (a
    send, a replay, an adoption, a release). ``tx["not_covered"]`` keeps the
    reason codes."""
    if verified is not True:
        return verified
    gaps = _gaps(ctx, w, result)
    if not gaps:
        return verified
    _note_gaps(tx, gaps)
    return None


def _post_image(images: Mapping[str, Any], volatile: Sequence[str]) -> dict[str, Any]:
    """What a committed statement left, as digests, for a later replay to
    read back: the rows it added or changed, and the rows it removed."""
    after = dict(images.get("after") or {})
    return {
        "after": after,
        "gone": sorted(r for r in images.get("before") or {} if r not in after),
        "volatile": list(volatile),
    }


def _sql_effect_holds(adapter: Any, image: Any) -> Optional[bool]:
    """Stale-cached-verdict class: a replay re-reads the database. True only
    when every row the statement left still reads as it committed and every
    row it removed is still gone; None when there is no image to compare."""
    if not isinstance(image, Mapping) or not isinstance(image.get("after"), Mapping):
        return None
    skip = set(image.get("volatile") or [])

    def kept(rid: str, cols: Mapping[str, str]) -> dict[str, str]:
        table = rid.split("/", 1)[0]
        return {c: d for c, d in cols.items() if f"{table}.{c}" not in skip}

    now_ = adapter.frame()
    for rid, want in image["after"].items():
        got = now_.get(rid)
        if got is None or kept(rid, got) != kept(rid, want):
            return False
    return not any(rid in now_ for rid in image.get("gone") or [])


def _readback(
    ctx: Ctx,
    w: Mapping[str, Any],
    base: Any,
    result: Mapping[str, Any],
    *,
    replay: bool = False,
) -> tuple[Optional[bool], Optional[dict]]:
    adapter = ctx.adapter(w["system"])
    if isinstance(adapter, MailAdapter):
        return None, None
    if w["op"] == "sql":
        if replay:
            # A replay sends nothing: its stored flag says what committed
            # then, not what the database holds now.
            if result.get("post_commit_matches") is not True:
                return False, None
            return _sql_effect_holds(adapter, result.get("post_image")), None
        # Committed only when its own diff matched the plan, and confirmed
        # only when the committed rows, read again, still match it.
        return result.get("post_commit_matches") is True, None
    rid = str(result.get("id") or w.get("id"))
    got = adapter.read(w["collection"], rid)
    if w["op"] == "delete":
        return got is None, None
    if got is None:
        return False, None
    want = adapter.expected_after(w, base) or {}
    ok = all(same(got.get(k), v) for k, v in want.items())
    return ok, got


def apply_plan(ctx: Ctx, digest: str) -> dict[str, Any]:
    started = now()
    state, spec = ctx.state, ctx.spec
    with state.lock():
        plan, priv = load_plan(state, digest)
        writes, baselines = priv["writes"], priv["baselines"]
        tx: dict[str, Any] = {"phase": "apply", "writes": len(writes)}

        def refuse(code: str, detail: str = "", approval: Any = None) -> TxRefused:
            tx["refused"] = code
            run = _write_run(
                state,
                digest,
                "apply",
                outcome="HALTED",
                results=[],
                tx=tx,
                approval=approval or state.approval_record(digest),
                started=started,
            )
            return TxRefused(code, detail, {"run_dir": str(run), "tx": tx})

        if plan["spec_sha256"] != _spec_sha(spec):
            raise refuse("spec_changed", "the tx file changed since the plan")
        # Every key before any send: a mismatched replay refuses the whole plan.
        for n, w in enumerate(writes):
            entry = state.entry(w["key"])
            if (
                entry is not None
                and entry.status not in FREE
                and entry.payload != w["payload_sha256"]
                and w.get("ledger") != "possible_replay"
            ):
                tx["blocked_double_effects"] = tx.get("blocked_double_effects", 0) + 1
        approval_peek = state.approval_record(digest) or {}
        for w in writes:
            if (
                w.get("ledger") == "possible_replay"
                and approval_peek.get("decided_by") != "human"
            ):
                tx["blocked_double_effects"] = tx.get("blocked_double_effects", 0) + 1
        if tx.get("blocked_double_effects"):
            raise refuse(
                "replay_mismatch",
                f"{tx['blocked_double_effects']} write(s) reuse a key already applied with a different payload",
            )
        # The frame must be what the plan read: anything else is a stale plan.
        now_frames = ctx.frames(list(priv["frames"]))
        moved = [
            s for s, f in now_frames.items() if sha256(f) != plan["frame_sha256"].get(s)
        ]
        if moved:
            raise refuse("plan_stale", f"{len(moved)} scope(s) changed since the plan")
        try:
            approval = state.consume_approval(digest)
        except StateError as exc:
            raise refuse(exc.code, exc.detail) from exc

        results: list[dict[str, Any]] = []
        images: dict[str, Any] = {}
        sent = 0
        stop: Optional[str] = None
        crash_after = int(os.environ.get(CRASH_ENV, "0") or 0)
        for w, base in zip(writes, baselines):
            key, payload = w["key"], w["payload_sha256"]
            entry = state.entry(key)
            if stop:
                results.append(
                    _line(
                        ok=False,
                        verified=None,
                        irreversible=w["irreversible"],
                        payload=payload,
                        code="not_sent",
                    )
                )
                continue
            if (
                w.get("ledger") == "possible_replay"
                and entry is not None
                and entry.status == "applied"
            ):
                entry = None  # a person approved it as new work: a new attempt under the key
            if entry is not None and entry.status not in FREE:
                if entry.status in ("applied", "held", "released"):
                    tx["replays"] = tx.get("replays", 0) + 1
                    # Unverified-success class: a replay sends nothing, so it
                    # is VERIFIED only if its effect reads back now. A held
                    # write is still waiting.
                    replay_verified: Optional[bool] = None
                    if entry.status == "held":
                        tx["held"] = tx.get("held", 0) + 1
                    else:
                        replay_verified, _ = _readback(
                            ctx, w, base, entry.result, replay=True
                        )
                        replay_verified = _capped(
                            ctx, w, replay_verified, tx, entry.result
                        )
                        if replay_verified is not True:
                            tx["unverified_replays"] = (
                                tx.get("unverified_replays", 0) + 1
                            )
                    results.append(
                        _line(
                            ok=True,
                            verified=replay_verified,
                            irreversible=w["irreversible"],
                            payload=payload,
                            code="replay_not_sent",
                        )
                    )
                    continue
                # begun / uncertain / undo_begun: an earlier attempt may have landed.
                verified, got = (
                    _readback(ctx, w, base, {"id": w["id"]})
                    if w["op"] == "update"
                    else (None, None)
                )
                # Nothing recorded what this attempt's send proved or what its
                # transaction could reach: the adapter's gaps for no result.
                adopted_verified = _capped(ctx, w, verified, tx, None)
                if verified:
                    state.record(
                        key,
                        "applied",
                        plan=digest,
                        payload=payload,
                        result={"id": w["id"]},
                        by="read_back",
                    )
                    images[key] = {
                        "result": {"id": w["id"]},
                        "after": _norm(spec.system(w["system"]), w, got),
                    }
                    results.append(
                        _line(
                            ok=True,
                            verified=adopted_verified,
                            irreversible=False,
                            payload=payload,
                            code="adopted_by_read_back",
                        )
                    )
                    continue
                state.record(
                    key,
                    "uncertain",
                    plan=digest,
                    payload=payload,
                    reason="open_attempt",
                )
                stop = "open_attempt"
                tx["held_uncertain"] = tx.get("held_uncertain", 0) + 1
                results.append(
                    _line(
                        ok=False,
                        verified=None,
                        irreversible=w["irreversible"],
                        payload=payload,
                        code="held_uncertain",
                    )
                )
                continue
            state.record(
                key,
                "begun",
                plan=digest,
                payload=payload,
                op=w["op"],
                slot=w["slot"],
                task_tag=state.tag("task:" + plan["task"]),
                rows=w.get("row_tags"),
            )
            if w["irreversible"]:
                release_at = now() + float(plan["holdback_s"])
                state.hold(
                    key,
                    {
                        "key": key,
                        "plan": digest,
                        "write": plan["writes"].index(
                            next(x for x in plan["writes"] if x["key"] == key)
                        ),
                        "release_at": release_at,
                    },
                )
                state.record(
                    key, "held", plan=digest, payload=payload, release_at=release_at
                )
                tx["held"] = tx.get("held", 0) + 1
                results.append(
                    _line(
                        ok=True,
                        verified=None,
                        irreversible=True,
                        payload=payload,
                        code="held",
                    )
                )
                continue
            try:
                result = ctx.adapter(w["system"]).apply(w, base, key)
            except StaleBaseline:
                # The server refused the conditional write: the record moved
                # since the plan read it, so nothing was written over it.
                state.record(
                    key,
                    "refused",
                    plan=digest,
                    payload=payload,
                    reason="stale_baseline",
                )
                stop = "stale_baseline"
                tx["stale_baseline"] = tx.get("stale_baseline", 0) + 1
                results.append(
                    _line(
                        ok=False,
                        verified=False,
                        irreversible=False,
                        payload=payload,
                        code="stale_baseline_not_written",
                    )
                )
                continue
            except Diverged:
                state.record(
                    key, "refused", plan=digest, payload=payload, reason="diverged"
                )
                stop = "diverged"
                tx["diverged"] = tx.get("diverged", 0) + 1
                results.append(
                    _line(
                        ok=False,
                        verified=False,
                        irreversible=False,
                        payload=payload,
                        code="diverged_rolled_back",
                    )
                )
                continue
            except AdapterError as exc:
                state.record(
                    key,
                    "uncertain",
                    plan=digest,
                    payload=payload,
                    reason=str(exc)[:120],
                )
                stop = "send_failed"
                results.append(
                    _line(
                        ok=False,
                        verified=None,
                        irreversible=False,
                        payload=payload,
                        code="send_failed",
                    )
                )
                continue
            sent += 1
            if crash_after and sent >= crash_after:
                os._exit(70)
            rows_image = result.pop("_images", None)
            if rows_image is not None:
                result["post_image"] = _post_image(rows_image, w.get("volatile") or [])
            state.record(key, "applied", plan=digest, payload=payload, result=result)
            verified, got = _readback(ctx, w, base, result)
            result.pop("post_image", None)
            images[key] = {
                "result": result,
                "after": _norm(spec.system(w["system"]), w, got),
            }
            if rows_image is not None:
                images[key]["rows"] = rows_image
            code = "confirmed" if verified else "refuted"
            if verified and _capped(ctx, w, verified, tx, result) is None:
                verified, code = None, "confirmed_not_covered"
            results.append(
                _line(
                    ok=verified is not False,
                    verified=verified,
                    irreversible=False,
                    payload=payload,
                    code=code,
                )
            )
        after = ctx.frames(list(priv["frames"]))
        missed: list[str] = []
        false: list[str] = []
        for scope in priv["frames"]:
            actual = diff_frames(priv["frames"][scope], after[scope])
            got = compare(plan["predicted"][scope], actual)
            missed += got["missed"]
            false += got["false"]
        tx.update(
            {
                "sent": sent,
                "confirmed": sum(1 for r in results if r["tx_code"] == "confirmed"),
                "refuted": sum(1 for r in results if r["tx_code"] == "refuted"),
                "missed_effects": len(missed),
                "false_diffs": len(false),
                "stopped": stop,
            }
        )
        write_private(
            state.plan_dir(digest) / "applied.json",
            {"images": images, "frames_after": after},
        )
        ok = not stop and not missed and not false and not tx["refuted"]
        outcome = (
            "VERIFIED"
            if ok
            and not tx.get("held")
            and not tx.get("unverified_replays")
            and not tx.get("capped")
            else ("COMPLETED_UNVERIFIED" if ok else "RECONCILIATION_REQUIRED")
        )
        run = _write_run(
            state,
            digest,
            "apply",
            outcome=outcome,
            results=results,
            tx=tx,
            approval=approval,
            started=started,
        )
        return {
            "outcome": outcome,
            "tx": tx,
            "run_dir": str(run),
            "receipt": receipt_status(run),
            "missed": missed,
            "false": false,
        }


# -- undo ----------------------------------------------------------------------


def undo_plan(ctx: Ctx, digest: str) -> dict[str, Any]:
    started = now()
    state, spec = ctx.state, ctx.spec
    with state.lock():
        plan, priv = load_plan(state, digest)
        if plan["spec_sha256"] != _spec_sha(spec):
            # Wrong-target class: undo goes only to the systems the plan named.
            raise TxRefused("spec_changed", "the tx file changed since the plan")
        applied_path = state.plan_dir(digest) / "applied.json"
        if not applied_path.exists():
            raise TxRefused("not_applied", digest[:12])
        images = read_json(applied_path)["images"]
        writes, baselines = priv["writes"], priv["baselines"]
        results: list[dict[str, Any]] = []
        per_write: dict[str, str] = {}
        uncovered: dict[str, list[str]] = {}
        tx: dict[str, Any] = {"phase": "undo", "writes": len(writes)}
        for w, base in reversed(list(zip(writes, baselines))):
            key = w["key"]
            entry = state.entry(key)
            system = spec.system(w["system"])
            if entry is None or entry.plan != digest:
                per_write[key] = "not_this_plan"
                continue
            if entry.status == "held":
                state.unhold(key)
                state.record(key, "cancelled", plan=digest, payload=entry.payload)
                per_write[key] = "cancelled"
                continue
            if entry.status == "cancelled":
                per_write[key] = "cancelled"
                continue
            if entry.status == "released":
                per_write[key] = "irreversible_released"
                continue
            if entry.status == "undone":
                per_write[key] = "already_undone"
                continue
            if entry.status != "applied":
                per_write[key] = "unresolved"
                continue
            result = entry.result or {"id": w.get("id")}
            adapter = ctx.adapter(w["system"])
            if w["op"] == "sql":
                per_write[key], gaps = _undo_sql(
                    state,
                    adapter,
                    w,
                    entry,
                    (images.get(key) or {}).get("rows"),
                    digest,
                )
                if per_write[key] == "undone" and gaps:
                    uncovered[key] = gaps
                continue
            rid = str(result.get("id") or w.get("id"))
            current = adapter.read(w["collection"], rid)
            want = (images.get(key) or {}).get("after")
            if w["op"] != "delete" and (
                want is None or _norm(system, w, current) != want
            ):
                per_write[key] = "changed_since_apply"
                continue
            state.record(key, "undo_begun", plan=digest, payload=entry.payload)
            try:
                gaps = adapter.compensate(
                    w,
                    base,
                    result,
                    key,
                    expect=(
                        None
                        if want is None
                        else (lambda rec, w=w, want=want: _norm(system, w, rec) == want)
                    ),
                )
            except (ChangedSinceApply, StaleBaseline):
                # The record moved after the check: nothing was written over it.
                per_write[key] = "changed_since_apply"
                continue
            except AdapterError as exc:
                state.record(
                    key,
                    "uncertain",
                    plan=digest,
                    payload=entry.payload,
                    reason=f"undo: {exc}"[:120],
                )
                per_write[key] = "undo_failed"
                continue
            back = adapter.read(
                w["collection"], rid if w["op"] != "delete" else str(w["id"])
            )
            if w["op"] == "create":
                ok = back is None
            else:
                ok = back is not None and _norm(system, w, back) == _norm(
                    system, w, base
                )
            if ok:
                state.record(
                    key, "undone", plan=digest, payload=entry.payload, read_back=ok
                )
            else:
                # Unverified-success class: a compensation the read-back
                # refutes is not undone, and keeps its replay key.
                state.record(
                    key,
                    "uncertain",
                    plan=digest,
                    payload=entry.payload,
                    reason="undo: the read-back does not show the baseline",
                )
            per_write[key] = "undone" if ok else "undo_refuted"
            if ok and gaps:
                # Closed-scope class: an undo sent with no enforced version
                # check (or with an open write scope) is not a verified undo.
                uncovered[key] = list(gaps)
        # The whole frame back to the baseline: the records and the collateral.
        after = ctx.frames(list(priv["frames"]))
        leftover: list[str] = []
        for scope in priv["frames"]:
            d = diff_frames(priv["frames"][scope], after[scope])
            leftover += d["added"] + d["removed"] + list(d["changed"])
        own = {}
        for w in writes:
            e = state.entry(w["key"])
            rid = f"{w['collection']}/{(e.result.get('id') if e else None) or w.get('id')}"
            own[rid] = w["key"]
        counted = [
            k
            for k, v in per_write.items()
            if v
            in (
                "undone",
                "undo_refuted",
                "frame_differs",
                "changed_since_apply",
                "undo_failed",
                "unresolved",
            )
        ]
        # Undo images name the rows a statement touched: those are the write's own.
        for w in writes:
            img = (images.get(w["key"]) or {}).get("rows") or {}
            for rid in [*img.get("before", {}), *img.get("added_pk", {})]:
                own.setdefault(rid, w["key"])
        collateral = [r for r in leftover if r not in own]
        for r in leftover:
            if r in own and per_write.get(own[r]) == "undone":
                per_write[own[r]] = "frame_differs"
        done = [k for k in counted if per_write[k] == "undone" and not collateral]
        verified = [k for k in done if k not in uncovered]
        for k in uncovered:
            if per_write.get(k) == "undone":
                per_write[k] = "undone_not_covered"
                _note_gaps(tx, uncovered[k])
        for w in writes:
            code = per_write.get(w["key"])
            if code in (
                None,
                "not_this_plan",
                "cancelled",
                "already_undone",
                "no_undo",
            ):
                continue
            results.append(
                _line(
                    ok=w["key"] in done,
                    verified=(w["key"] in verified)
                    if code not in ("irreversible_released", "undone_not_covered")
                    else None,
                    irreversible=w["irreversible"],
                    payload=w["payload_sha256"],
                    code=code,
                )
            )
        tx.update(
            {
                "undo_supported": len(counted),
                "undo_verified": len(verified),
                "cancelled": sum(1 for v in per_write.values() if v == "cancelled"),
                "irreversible_released": sum(
                    1 for v in per_write.values() if v == "irreversible_released"
                ),
                "no_generated_undo": sum(
                    1 for v in per_write.values() if v == "no_undo"
                ),
                "collateral_changes": len(collateral),
                "codes": {
                    c: sum(1 for v in per_write.values() if v == c)
                    for c in sorted(set(per_write.values()))
                },
            }
        )
        # An applied write with no undo (DDL, or no undo images) is still in
        # place: the plan is not rolled back, whatever the rows show.
        not_undone = [
            k for k, v in per_write.items() if v in ("no_undo", "irreversible_released")
        ]
        ok = len(done) == len(counted) and not collateral and not not_undone
        outcome = (
            ("ROLLED_BACK" if len(verified) == len(done) else "COMPLETED_UNVERIFIED")
            if ok
            else "RECONCILIATION_REQUIRED"
        )
        approval = state.approval_record(digest)
        run = _write_run(
            state,
            digest,
            "undo",
            outcome=outcome,
            results=results,
            tx=tx,
            approval=approval,
            started=started,
        )
        return {
            "outcome": outcome,
            "tx": tx,
            "run_dir": str(run),
            "receipt": receipt_status(run),
            "collateral": collateral,
        }


# -- holdback ------------------------------------------------------------------


def cancel(ctx: Ctx, digest: str) -> int:
    state = ctx.state
    n = 0
    with state.lock():
        for item in state.held():
            if item["plan"] != digest:
                continue
            entry = state.entry(item["key"])
            state.unhold(item["key"])
            state.record(
                item["key"],
                "cancelled",
                plan=digest,
                payload=entry.payload if entry else "",
            )
            n += 1
    return n


def release(ctx: Ctx, digest: str, *, at: Optional[float] = None) -> dict[str, Any]:
    """Release the plan's held writes whose holdback has passed."""
    started = now()
    state = ctx.state
    at = now() if at is None else at
    with state.lock():
        plan, priv = load_plan(state, digest)
        if plan["spec_sha256"] != _spec_sha(ctx.spec):
            # Wrong-target class: a release goes only where the plan said.
            raise TxRefused("spec_changed", "the tx file changed since the plan")
        writes = {w["key"]: (w, b) for w, b in zip(priv["writes"], priv["baselines"])}
        results = []
        tx: dict[str, Any] = {
            "phase": "release",
            "released": 0,
            "waiting": 0,
            "adopted": 0,
            "double": 0,
        }
        for item in state.held():
            if item["plan"] != digest:
                continue
            if item["release_at"] > at:
                tx["waiting"] += 1
                continue
            key = item["key"]
            w, base = writes[key]
            adapter = ctx.adapter(w["system"])
            prior = state.entry(key)
            # Double-send class: a release that began before may have landed
            # even if the target doesn't show it yet. Never send it again.
            retried = prior is not None and any(
                e.get("event") == "release_begun" for e in prior.events
            )
            state.record(key, "release_begun", plan=digest, payload=w["payload_sha256"])
            verified: Optional[bool]
            if isinstance(adapter, MailAdapter):
                before = adapter.count(key)
                if before == 0 and retried:
                    before = adapter.wait_count(key, 1)
                if before == 0 and retried:
                    state.record(
                        key,
                        "uncertain",
                        plan=digest,
                        payload=w["payload_sha256"],
                        reason="an earlier release may have sent it",
                    )
                    results.append(
                        _line(
                            ok=False,
                            verified=None,
                            irreversible=True,
                            payload=w["payload_sha256"],
                            code="release_uncertain",
                        )
                    )
                    continue
                if before == 0:
                    try:
                        adapter.send(w, key)
                    except AdapterError:
                        state.record(
                            key,
                            "uncertain",
                            plan=digest,
                            payload=w["payload_sha256"],
                            reason="smtp",
                        )
                        results.append(
                            _line(
                                ok=False,
                                verified=None,
                                irreversible=True,
                                payload=w["payload_sha256"],
                                code="send_failed",
                            )
                        )
                        continue
                else:
                    tx["adopted"] += 1
                n = adapter.wait_count(key, 1)
                tx["double"] += int(n > 1)
                verified = n == 1
            else:
                # Stale-baseline class: the record must still be what the
                # plan read, or the held write would clobber a newer edit.
                if w.get("id") is not None and w["op"] in ("update", "delete"):
                    current = adapter.read(w["collection"], str(w["id"]))
                    system = ctx.spec.system(w["system"])
                    if current is None or _norm(system, w, current) != _norm(
                        system, w, base
                    ):
                        state.record(
                            key,
                            "uncertain",
                            plan=digest,
                            payload=w["payload_sha256"],
                            reason="changed during holdback",
                        )
                        results.append(
                            _line(
                                ok=False,
                                verified=None,
                                irreversible=True,
                                payload=w["payload_sha256"],
                                code="changed_during_holdback",
                            )
                        )
                        continue
                try:
                    sent_result = adapter.apply(w, base, key)
                except StaleBaseline:
                    # The server refused the conditional write: the record
                    # changed after the recheck read. Nothing was written.
                    state.record(
                        key,
                        "uncertain",
                        plan=digest,
                        payload=w["payload_sha256"],
                        reason="changed during holdback",
                    )
                    results.append(
                        _line(
                            ok=False,
                            verified=None,
                            irreversible=True,
                            payload=w["payload_sha256"],
                            code="changed_during_holdback",
                        )
                    )
                    continue
                verified, _ = _readback(ctx, w, base, {"id": w.get("id")})
                # Stale-baseline class: the recheck read and the send are two
                # requests; only an enforced version check closes the gap.
                verified = _capped(ctx, w, verified, tx, sent_result)
            state.unhold(key)
            state.record(
                key,
                "released",
                plan=digest,
                payload=w["payload_sha256"],
                read_back=verified,
            )
            tx["released"] += 1
            unconditional_release = verified is None and not isinstance(
                adapter, MailAdapter
            )
            results.append(
                _line(
                    ok=bool(verified) or unconditional_release,
                    verified=verified,
                    irreversible=True,
                    payload=w["payload_sha256"],
                    code=(
                        "released"
                        if verified
                        else (
                            "released_unconditional"
                            if unconditional_release
                            else "release_refuted"
                        )
                    ),
                )
            )
        outcome = (
            "VERIFIED"
            if results and all(r["effect_verified"] for r in results)
            else (
                "COMPLETED_UNVERIFIED"
                if not results
                or all(
                    r["effect_verified"] or r["tx_code"] == "released_unconditional"
                    for r in results
                )
                else "RECONCILIATION_REQUIRED"
            )
        )
        run = _write_run(
            state,
            digest,
            "release",
            outcome=outcome,
            results=results,
            tx=tx,
            approval=state.approval_record(digest),
            started=started,
        )
        return {
            "outcome": outcome,
            "tx": tx,
            "run_dir": str(run),
            "receipt": receipt_status(run),
        }


def summary_lines(plan: Mapping[str, Any]) -> list[str]:
    """The plan as a person reads it (terminal only: may show values)."""
    lines = [f"plan {plan['digest'][:12]} for task {plan['task']}"]
    for what, n in sorted(plan["summary"].items()):
        lines.append(f"  {n} {what}")
    if plan.get("queued"):
        lines.append(
            f"  irreversible writes wait {plan['holdback_s']:.0f}s in a cancelable holdback queue"
        )
    for system, how in sorted(plan["dry_run"].items()):
        lines.append(f"  {system}: diff from {how.replace('_', ' ')}")
    for scope, pred in sorted(plan["predicted"].items()):
        added = sum(pred["added"].values())
        lines.append(
            f"  {scope}: predicted {added} added, {len(pred['changed'])} changed, {len(pred['removed'])} removed"
        )
        tables = (
            _counts(
                {
                    "added": [
                        f"{t}/+{i}" for t, n in pred["added"].items() for i in range(n)
                    ],
                    "removed": pred["removed"],
                    "changed": pred["changed"],
                }
            )
            or {}
        )
        if len(tables) > 1 or scope.endswith(":*"):
            for table, c in sorted(tables.items()):
                parts = [f"{v} rows {k}" for k, v in c.items() if v]
                lines.append(f"    {table}: {', '.join(parts)}")
    details = plan.get("anomaly_details") or plan["anomalies"]
    lines.append(f"  {len(details)} anomal{'y' if len(details) == 1 else 'ies'}")
    for a in details:
        where = "" if a.get("write") is None else f" (write {a['write']})"
        value = "" if a.get("value") is None else f": {a.get('detail')} = {a['value']}"
        lines.append(f"    ! {a['label']}{value}{where}")
    return lines
