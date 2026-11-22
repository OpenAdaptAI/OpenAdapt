"""Resource-scoped writes for API and SaaS systems, where there is no database.

A whole-system diff can't be read from a SaaS API. This path checks what can
be checked and says what can't:

1. **Pre-read** the target record (or, for a create, search for the
   idempotency marker), and take a cursor on the system's own log.
2. **Send once.** The idempotency key is derived from the task, the record
   and the operation (:meth:`State.key_for`), never from the payload, and is
   written to the ledger before the send. It goes out as ``Idempotency-Key``
   (``idempotency_header``) when the API honors one, and as a marker or a
   conditional-create header when the collection declares one. A retry with
   the same payload returns the stored result; a retry whose payload was
   regenerated differently is refused (``replay_mismatch``). A crash between
   the send and the ledger is recovered by finding the record (marker search
   or the log), not by sending again.
3. **Read the record back**, normalize it with the collection's codecs
   (``null_empty`` and the rest from :mod:`openadapt_flow.tx.judge`) and
   compare it to the intended fields; on an update, every other field must be
   unchanged.
4. **Cross-check the log**: the write's own event must appear, and no other
   resource may have been changed by the same actor in the window.

Verdicts: a wrong value, a missing record, a duplicate, a changed field
outside the intent, or another resource changed by the same actor is a hard
failure (``FAILED``). An unknown event type is logged, never a stop. A slow
read-back or a log that trails the write is retried; if it never settles the
result is ``COMPLETED_UNVERIFIED``. Every outcome writes a ``record/run/v1``
receipt.

The scope is the resource and the actor, not the system: a change the log
doesn't record, or one made by another actor, is outside it.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Callable, Mapping, Optional

from openadapt_flow.txplan.adapters import AdapterError, RestAdapter
from openadapt_flow.txplan.audit import (
    Audit,
    AuditError,
    AuditReader,
    Cursor,
    ScopedCollection,
    check_window,
    fill,
    fill_json,
    pick,
    prefix_of,
)
from openadapt_flow.txplan.engine import Ctx, TxRefused, _line, _write_run
from openadapt_flow.txplan.spec import Collection, RestSystem, TxSpecError
from openadapt_flow.txplan.state import now, sha256

SCOPED_SCHEMA = "openadapt.tx-scoped/v1"
#: Hard failures: the write is wrong, whatever else is true.
HARD = (
    "wrong_value",
    "partial_write",
    "frame_clobbered",
    "missing_resource",
    "still_present",
    "duplicate_write",
    "collateral_mutation",
)
#: Not verified: the read-back or the log never settled.
SOFT = ("audit_lag", "audit_unreadable", "recovery_unproven")

Send = Callable[..., Optional[str]]
_ABSENT = object()


def marker_for(key: str) -> str:
    """The value a create carries so a retry can find it (not the key itself)."""
    return "oa-" + hashlib.sha256(key.encode()).hexdigest()[:24]


def _norm_identity(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return value


def create_slot(
    scoped: ScopedCollection, fields: Mapping[str, Any], slot: Optional[str]
) -> str:
    if slot:
        return str(slot)
    if scoped.identity:
        ident = {k: _norm_identity(fields.get(k)) for k in scoped.identity}
        if all(v in (None, "") for v in ident.values()):
            raise TxSpecError(
                "the create holds none of the collection's identity fields"
            )
        return "id-" + sha256(ident)[:24]
    return "1"


# -- HTTP ------------------------------------------------------------------------


def _form(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    if isinstance(value, Mapping):
        return [
            kv
            for k, v in value.items()
            for kv in _form(v, f"{prefix}[{k}]" if prefix else str(k))
        ]
    if isinstance(value, list):
        return [kv for i, v in enumerate(value) for kv in _form(v, f"{prefix}[{i}]")]
    if value is None:
        return [(prefix, "")]
    if isinstance(value, bool):
        return [(prefix, "true" if value else "false")]
    return [(prefix, str(value))]


def _request(
    adapter: RestAdapter,
    method: str,
    path: str,
    body: Any = None,
    headers: Optional[Mapping[str, str]] = None,
    encoding: str = "json",
) -> tuple[int, Any]:
    kw: dict[str, Any] = {"json": body}
    if encoding == "form" and body is not None:
        kw = {"data": dict(_form(body))}
    try:
        resp = adapter.http.request(method, path, headers=dict(headers or {}), **kw)
    except Exception as exc:  # noqa: BLE001 - delivery unknown
        raise AdapterError(f"{method} failed: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        raise AdapterError(f"{method} returned {resp.status_code}")
    try:
        return resp.status_code, (resp.json() if resp.content else {})
    except ValueError:
        if method.upper() == "GET":
            # Missing-evidence class: an unreadable read is an error, not {}.
            raise AdapterError(f"{method} returned a body that is not JSON") from None
        return resp.status_code, {}


def default_send(
    adapter: RestAdapter,
    coll: Collection,
    *,
    op: str,
    fields: Mapping[str, Any],
    rid: Optional[str],
    baseline: Optional[Mapping[str, Any]],
    key: str,
    marker: Optional[str],
    path: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Make the write once; return the record's id."""
    scoped = coll.scoped
    assert scoped is not None
    route = getattr(coll, op, None)
    if route is None:
        raise AdapterError(f"{coll.name} declares no {op}")
    headers: dict[str, str] = {}
    if adapter.system.idempotency_header:
        headers[adapter.system.idempotency_header] = key
    values = {**(baseline or {}), **fields, **(path or {}), "id": rid}
    if op == "create":
        body: Any = {**scoped.create_body, **fields}
        if marker and scoped.marker:
            body.update(fill_json(scoped.marker, {"marker": marker}))
        if marker and scoped.condition_header:
            headers.update(
                {
                    k: fill(v, {"marker": marker})
                    for k, v in scoped.condition_header.items()
                }
            )
    elif op == "delete":
        body = None
    else:
        body = adapter._body(coll, route, dict(baseline) if baseline else None, fields)
    _, got = _request(
        adapter,
        route.method,
        adapter._fill(route.path, values),
        body,
        headers,
        scoped.encoding,
    )
    if op == "create":
        new = (got or {}).get(route.id_from or coll.id_field)
        if new in (None, ""):
            raise AdapterError("the create returned no id")
        return str(new)
    return rid


# -- checks ----------------------------------------------------------------------


def _codecs(scoped: ScopedCollection) -> dict[str, Any]:
    from openadapt_flow.tx.types import FieldCodec

    return {path: FieldCodec(kind=kind) for path, kind in scoped.codecs.items()}  # type: ignore[arg-type]


def judge_record(
    coll: Collection,
    *,
    op: str,
    baseline: Optional[Mapping[str, Any]],
    after: Optional[Mapping[str, Any]],
    fields: Mapping[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    """The read-back's codes: empty when the record reads back as intended."""
    from openadapt_flow.tx.judge import FrameAndIntentJudge, flatten_record

    scoped = coll.scoped
    assert scoped is not None
    if op == "delete":
        return (["still_present"] if after is not None else []), {}
    if after is None:
        return ["missing_resource"], {}
    volatile = tuple(scoped.volatile) + tuple(coll.server_fields)
    if op == "create":
        # A create's server fields are the server's: only the intent is judged.
        volatile = volatile + ("*",)
    verdict = FrameAndIntentJudge().evaluate(
        None if op == "create" else baseline,
        after,
        fields,
        volatile_fields=volatile,
        field_codecs=_codecs(scoped),
    )
    detail = {
        "mismatched_fields": sorted(verdict.mismatched_fields),
        "clobbered_fields": sorted(verdict.clobbered_fields),
    }
    codes: list[str] = []
    if verdict.mismatched_fields:
        # A field still at its old value (or absent on a create) was not
        # written: a partial write. A field at some third value is wrong.
        before = flatten_record(baseline) if baseline and op != "create" else {}
        now_ = flatten_record(after)

        def _unwritten(p: str) -> bool:
            got = now_.get(p, _ABSENT)
            if op == "create":
                return got is _ABSENT or got in (None, "", [], {})
            return got == before.get(p, _ABSENT)

        unwritten = [p for p in verdict.mismatched_fields if _unwritten(p)]
        if len(unwritten) < len(verdict.mismatched_fields):
            codes.append("wrong_value")
        if unwritten:
            codes.append("partial_write")
    if verdict.clobbered_fields:
        codes.append("frame_clobbered")
    if not codes and not verdict.passed:
        codes.append("wrong_value")
    return codes, detail


def _search(
    adapter: RestAdapter, scoped: ScopedCollection, marker: str, id_field: str
) -> Optional[list[str]]:
    if not scoped.search:
        return None
    _, got = _request(adapter, "GET", fill(scoped.search, {"marker": marker}))
    # Missing-evidence class: a search response of the wrong shape is an
    # error, never an empty result.
    if scoped.search_items and not isinstance(got, Mapping):
        raise AdapterError("the marker search did not return an object")
    head = scoped.search_items.split(".", 1)[0] if scoped.search_items else ""
    empty = scoped.search_empty
    # The declared empty shape counts only when the answer claims no matches:
    # one that reports a total above zero but carries no items is partial,
    # never empty.
    declared_empty = (
        bool(empty)
        and got.get("total") in (None, 0)
        and any(
            str(v) == str(empty.get("equals"))
            for v in pick(got, str(empty.get("path", "")))
        )
    )
    if head and head not in got and got.get("total") != 0 and not declared_empty:
        # Missing-evidence class: no results field is not an empty result,
        # unless the response says it matched nothing (total 0).
        raise AdapterError(f"the marker search has no {head}")
    if not scoped.search_items and not isinstance(got, list):
        raise AdapterError("the marker search did not return a list")
    items = pick(got, scoped.search_items) if scoped.search_items else got
    if scoped.search_items and len(items) == 1 and isinstance(items[0], list):
        items = items[0]
    return [
        str(x.get(id_field))
        for x in items
        if isinstance(x, Mapping) and x.get(id_field) is not None
    ]


def _read(
    adapter: RestAdapter, coll: Collection, rid: str, where: Mapping[str, Any]
) -> Optional[dict[str, Any]]:
    got = adapter._call(
        coll.read.method, adapter._fill(coll.read.path, {**where, "id": rid})
    )
    if got is not None and not isinstance(got, dict):
        raise AdapterError("a read did not return an object")
    return got


def _ref(
    scoped: ScopedCollection,
    fields: Mapping[str, Any],
    where: Mapping[str, Any],
    rid: Optional[str],
) -> str:
    return fill(scoped.audit_ref, {**fields, **where, "id": rid})


def _matches(
    coll: Collection, record: Optional[Mapping[str, Any]], fields: Mapping[str, Any]
) -> bool:
    codes, _ = judge_record(
        coll, op="create", baseline=None, after=record, fields=fields
    )
    return not codes


def _rid_of(ref: str, scoped: ScopedCollection) -> Optional[str]:
    """The record id inside an audit reference (``Patient/7`` -> ``7``)."""
    if "{id}" not in scoped.audit_ref:
        return None
    head, _, tail = scoped.audit_ref.partition("{id}")
    if not ref.startswith(head) or (tail and not ref.endswith(tail)):
        return None
    return ref[len(head) : len(ref) - len(tail) if tail else None] or None


# -- the write -------------------------------------------------------------------


def scoped_write(
    ctx: Ctx,
    *,
    task: str,
    system: str,
    collection: str,
    op: str,
    fields: Optional[Mapping[str, Any]] = None,
    rid: Optional[str] = None,
    slot: Optional[str] = None,
    path: Optional[Mapping[str, Any]] = None,
    send: Optional[Send] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Make one write to an API system and verify it on the resource and the log.

    ``path`` holds values for the routes' placeholders that are not record
    fields (the issue a comment belongs to): they are neither sent nor
    compared. ``send`` replaces :func:`default_send` (a test or a fault plant
    uses it); it gets the same arguments and returns the record id.
    """
    started = now()
    spec_system = ctx.spec.system(system)
    if not isinstance(spec_system, RestSystem):
        raise TxSpecError(f"system {system} is not a REST system")
    coll = spec_system.collection(collection)
    scoped = coll.scoped
    if scoped is None:
        raise TxSpecError(f"{system}/{collection} declares no scoped verification")
    if op not in ("create", "update", "delete"):
        raise TxSpecError(f"op {op!r} is not create, update or delete")
    if op != "create" and not rid:
        raise TxSpecError(f"an {op} needs the record id")
    if op == "delete" and not coll.delete_reversible:
        # Skipped-holdback class: a delete the API can't undo waits in the
        # plan's holdback queue; a scoped write never sends it straight away.
        raise TxSpecError(
            f"{system}/{collection} deletes are irreversible: use a plan (holdback)"
        )
    fields = dict(fields or {})
    where = {str(k): v for k, v in (path or {}).items()}
    adapter: RestAdapter = ctx.adapter(system)
    reader = AuditReader(spec_system.audit, adapter.http) if spec_system.audit else None
    state = ctx.state
    # An update or delete is keyed on its record; ``slot`` names a second,
    # deliberate write to the same record within one task (``step-2``).
    the_slot = (
        create_slot(scoped, {**where, **fields}, slot)
        if op == "create"
        else str(rid) + (f":{slot}" if slot else "")
    )
    # Wrong-target class: a cached result belongs to one endpoint, so the same
    # system name pointed somewhere else is a different key.
    # Wrong-target class: the account counts too, by a digest of the
    # credential, so two accounts on one endpoint never share a result.
    secret = os.environ.get(spec_system.auth.value_env, "") if spec_system.auth else ""
    account = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]
    key = state.key_for(
        task=task,
        system=f"{system}@{spec_system.url()}#{account}",
        collection=collection,
        op=op,
        slot=the_slot,
    )
    payload = sha256({"op": op, "id": rid, "path": where, "fields": fields})
    digest = sha256(
        {"schema": SCOPED_SCHEMA, "task": task, "key": key, "payload": payload}
    )
    marker = marker_for(key) if op == "create" else None
    sender = send or default_send
    notes: list[str] = []
    tx: dict[str, Any] = {
        "schema": SCOPED_SCHEMA,
        "system": system,
        "collection": collection,
        "op": op,
        "key": key,
        "audit_pattern": spec_system.audit.pattern if spec_system.audit else None,
    }

    with state.lock():
        entry = state.entry(key)
        if entry is not None and entry.payload != payload:
            tx["codes"] = ["replay_mismatch"]
            run = _write_run(
                state,
                digest,
                "api",
                outcome="HALTED",
                results=[],
                tx=tx,
                approval=None,
                started=started,
            )
            raise TxRefused(
                "replay_mismatch",
                "this task already wrote this record with a different payload; nothing was sent",
                {"run_dir": str(run)},
            )
        if entry is not None and entry.status == "resolved":
            out = dict(entry.result)
            out["replayed"] = True
            return out

        baseline = None
        recovering = entry is not None and entry.status in ("begun", "sent")
        if op != "create":
            baseline = _read(adapter, coll, str(rid), where)
            # A crash after a delete landed leaves no record: recover it below,
            # don't halt as if nothing was ever sent.
            if baseline is None and not recovering:
                tx["codes"] = ["missing_resource"]
                run = _write_run(
                    state,
                    digest,
                    "api",
                    outcome="HALTED",
                    results=[],
                    tx=tx,
                    approval=None,
                    started=started,
                )
                raise TxRefused(
                    "missing_resource",
                    f"{collection}/{rid} does not exist; nothing was sent",
                    {"run_dir": str(run)},
                )

        sent_id: Optional[str] = None
        cursor: Optional[Cursor] = None
        skip_send = False
        if entry is not None and entry.status in ("begun", "sent"):
            # A crash between the send and the ledger: find the write, don't repeat it.
            begun = next(e for e in reversed(entry.events) if e["event"] == "begun")
            cursor = Cursor.from_json(begun["cursor"]) if begun.get("cursor") else None
            baseline = begun.get("baseline", baseline)
            found, proven_absent = _recover(
                adapter,
                coll,
                reader,
                cursor,
                op,
                fields,
                rid,
                where,
                marker,
                sleep,
                baseline=baseline,
            )
            if found is not None:
                sent_id, skip_send = found, True
                notes.append("recovered_without_resend")
            elif not proven_absent:
                tx["codes"] = ["recovery_unproven"]
                return _finish(
                    ctx,
                    digest,
                    key,
                    tx,
                    started,
                    outcome="COMPLETED_UNVERIFIED",
                    verified=None,
                    payload=payload,
                    result={"id": None, "codes": ["recovery_unproven"]},
                    record_key=False,
                )
            else:
                notes.append("resent_after_proven_absent")
        if cursor is None and reader is not None and not skip_send:
            try:
                cursor = reader.cursor(sleep=sleep)
            except AuditError:
                # Missing-evidence class: with a log configured, a write it
                # can't place is never sent.
                tx["codes"] = ["audit_cursor_unreadable"]
                return _finish(
                    ctx,
                    digest,
                    key,
                    tx,
                    started,
                    outcome="HALTED",
                    verified=None,
                    payload=payload,
                    result={"id": None, "codes": ["audit_cursor_unreadable"]},
                    record_key=False,
                )
        if op == "create" and not skip_send and marker and scoped.search:
            # The ledger may be new (another computer, a lost state folder):
            # the marker still finds a create that already landed.
            existing = _search(adapter, scoped, marker, coll.id_field) or []
            if existing:
                sent_id, skip_send = existing[0], True
                notes.append("marker_found_before_send")
        if entry is None:
            # Durable before the send: a crash after it leaves a ``begun`` to recover.
            state.record(
                key,
                "begun",
                plan=digest,
                payload=payload,
                cursor=cursor.to_json() if cursor else None,
                baseline=baseline,
            )
        if not skip_send:
            try:
                sent_id = sender(
                    adapter,
                    coll,
                    op=op,
                    fields=fields,
                    rid=rid,
                    baseline=baseline,
                    key=key,
                    marker=marker,
                    path=where,
                )
            except AdapterError as exc:
                state.record(key, "sent", result={"id": None, "error": str(exc)[:200]})
                tx["codes"] = ["send_failed"]
                return _finish(
                    ctx,
                    digest,
                    key,
                    tx,
                    started,
                    outcome="COMPLETED_UNVERIFIED",
                    verified=None,
                    payload=payload,
                    result={
                        "id": None,
                        "codes": ["send_failed"],
                        "error": str(exc)[:200],
                    },
                    record_key=False,
                )
            state.record(key, "sent", result={"id": sent_id})

        target_id = sent_id if op == "create" else str(rid)
        codes, detail = _verify(
            adapter,
            coll,
            reader,
            cursor,
            op=op,
            fields=fields,
            baseline=baseline,
            rid=target_id,
            where=where,
            marker=marker,
            notes=notes,
            sleep=sleep,
        )
        hard = [c for c in codes if c in HARD]
        soft = [c for c in codes if c in SOFT]
        if hard:
            outcome, verified = "FAILED", False
        elif soft:
            outcome, verified = "COMPLETED_UNVERIFIED", None
        else:
            outcome, verified = "VERIFIED", True
        tx.update({"codes": codes, "notes": notes, **detail})
        result = {"id": target_id, "outcome": outcome, "codes": codes, "notes": notes}
        return _finish(
            ctx,
            digest,
            key,
            tx,
            started,
            outcome=outcome,
            verified=verified,
            payload=payload,
            result=result,
        )


def _finish(
    ctx: Ctx,
    digest: str,
    key: str,
    tx: Mapping[str, Any],
    started: float,
    *,
    outcome: str,
    verified: Optional[bool],
    payload: str,
    result: dict[str, Any],
    record_key: bool = True,
) -> dict[str, Any]:
    line = _line(
        ok=verified is True,
        verified=verified,
        irreversible=False,
        payload=payload,
        code=",".join(result.get("codes") or []) or "verified",
    )
    # Nothing here is approved: an unsettled check is not an approved write.
    line["effect_approved_unverified"] = False
    run = _write_run(
        ctx.state,
        digest,
        "api",
        outcome=outcome,
        results=[line],
        tx=tx,
        approval=None,
        started=started,
    )
    result = {
        **result,
        "outcome": outcome,
        "run_dir": str(run),
        "receipt": (run / "receipt.json").is_file(),
    }
    if record_key:
        ctx.state.record(key, "resolved", result=result)
    return result


def _readback(
    adapter: RestAdapter,
    coll: Collection,
    *,
    op: str,
    rid: Optional[str],
    where: Mapping[str, Any],
    baseline: Optional[Mapping[str, Any]],
    fields: Mapping[str, Any],
    sleep: Callable[[float], None],
) -> tuple[list[str], dict[str, Any], int]:
    scoped = coll.scoped
    assert scoped is not None
    codes: list[str] = ["missing_resource"]
    detail: dict[str, Any] = {}
    tries = 0
    for tries in range(1, scoped.readback_attempts + 1):
        after = _read(adapter, coll, str(rid), where) if rid else None
        codes, detail = judge_record(
            coll, op=op, baseline=baseline, after=after, fields=fields
        )
        if not codes:
            break
        sleep(scoped.readback_wait_s)
    return codes, detail, tries


def _verify(
    adapter: RestAdapter,
    coll: Collection,
    reader: Optional[AuditReader],
    cursor: Optional[Cursor],
    *,
    op: str,
    fields: Mapping[str, Any],
    baseline: Optional[Mapping[str, Any]],
    rid: Optional[str],
    where: Mapping[str, Any],
    marker: Optional[str],
    notes: list[str],
    sleep: Callable[[float], None],
) -> tuple[list[str], dict[str, Any]]:
    scoped = coll.scoped
    assert scoped is not None
    codes, detail, tries = _readback(
        adapter,
        coll,
        op=op,
        rid=rid,
        where=where,
        baseline=baseline,
        fields=fields,
        sleep=sleep,
    )
    detail["readback_attempts"] = tries
    if marker and scoped.search:
        found = _search(adapter, scoped, marker, coll.id_field) or []
        if len(found) > 1:
            codes.append("duplicate_write")
    if reader is None:
        notes.append("no_audit_log")
        return codes, detail
    if cursor is None:
        codes.append("audit_unreadable")
        return codes, detail
    target = _ref(scoped, fields, where, rid)
    logged = op in reader.source.logs
    if op == "update" and scoped.logged_fields is not None:
        logged = logged and any(f in scoped.logged_fields for f in fields)
    if op == "update" and baseline is not None and _matches(coll, baseline, fields):
        # The record already held the intent: a server may skip a write that
        # changes nothing, and log nothing for it.
        logged = False
        notes.append("no_change_update")
    try:
        audit: Audit = check_window(
            reader,
            cursor,
            op=op,  # type: ignore[arg-type]
            target=target,
            collection_prefix=prefix_of(scoped.audit_ref),
            logged=logged,
            sleep=sleep,
        )
    except AuditError as exc:
        codes.append("audit_unreadable")
        detail["audit_error"] = str(exc)[:200]
        return codes, detail
    detail["audit"] = audit.summary()
    if not audit.caught_up:
        codes.append("audit_lag")
    if not logged and "no_change_update" not in notes:
        notes.append(f"log_does_not_record_{op}")
    own_ops = [e for e in audit.own if e.kind == op]
    if len(own_ops) > 1:
        # Two creates under one reference (two comments on one issue) or two
        # updates of the record: the write landed twice.
        codes.append("duplicate_write")
    if any(
        e.kind != op and e.kind in ("create", "update", "delete") for e in audit.own
    ):
        # The record changed in another way too (a delete after an update).
        codes.append("frame_clobbered")
    for e in audit.same_collection_creates:
        other = next((x for x in (_rid_of(r, scoped) for r in e.resources) if x), None)
        record = _read(adapter, coll, other, where) if other else None
        if record is not None and _matches(coll, record, fields):
            codes.append("duplicate_write")
        else:
            codes.append("collateral_mutation")
    if audit.collateral:
        codes.append("collateral_mutation")
    if audit.unknown:
        notes.append("unknown_events_logged")
    seen: list[str] = []
    for c in codes:
        if c not in seen:
            seen.append(c)
    return seen, detail


def _recover(
    adapter: RestAdapter,
    coll: Collection,
    reader: Optional[AuditReader],
    cursor: Optional[Cursor],
    op: str,
    fields: Mapping[str, Any],
    rid: Optional[str],
    where: Mapping[str, Any],
    marker: Optional[str],
    sleep: Callable[[float], None],
    *,
    baseline: Optional[Mapping[str, Any]] = None,
) -> tuple[Optional[str], bool]:
    """After a crash mid-write: the record's id if the write landed, and
    whether it is proven not to have landed."""
    scoped = coll.scoped
    assert scoped is not None
    if op != "create":
        after = _read(adapter, coll, str(rid), where)
        if op == "delete":
            return (str(rid), False) if after is None else (None, True)
        if _matches(coll, after, fields):
            return str(rid), False
        # Stale-baseline class: resend only over the record as it was read
        # before the crash; another caller's edit since is never overwritten.
        unchanged = (
            after is not None and baseline is not None and dict(after) == dict(baseline)
        )
        return None, unchanged
    attempts = reader.source.lag.attempts if reader is not None else 1
    wait = reader.source.lag.wait_s if reader is not None else 0.0
    if marker and scoped.search:
        # Missing-evidence class: one empty search isn't proof of absence;
        # every poll through the lag budget must come back empty.
        for n in range(attempts):
            found = _search(adapter, scoped, marker, coll.id_field) or []
            if found:
                return found[0], False
            if n + 1 < attempts:
                sleep(wait)
        return None, reader is not None
    if reader is not None and cursor is not None and "create" in reader.source.logs:
        prefix = prefix_of(scoped.audit_ref)
        complete = 0
        unmatched_create = False
        for _ in range(reader.source.lag.attempts):
            try:
                win = reader.after(cursor)
            except AuditError:
                sleep(reader.source.lag.wait_s)
                continue
            complete += 1
            for e in win.events:
                if e.kind != "create":
                    continue
                if not e.resources:
                    # A create naming no resource may be ours: no proof.
                    unmatched_create = True
                for r in e.resources:
                    if prefix and not r.startswith(prefix):
                        continue
                    other = _rid_of(r, scoped)
                    if other and _matches(
                        coll, _read(adapter, coll, other, where), fields
                    ):
                        return other, False
                    # A create by these credentials we can't tie to a record
                    # (a comment logged by its parent issue) may be ours.
                    unmatched_create = True
            sleep(reader.source.lag.wait_s)
        # Proven absent only when every poll read the log to the cursor and
        # no create by these credentials appeared in it.
        return None, complete == reader.source.lag.attempts and not unmatched_create
    return None, False


# -- MCP tools -------------------------------------------------------------------

TOOLS = [
    {
        "name": "api_read",
        "description": "Read one record from an API system OpenAdapt holds the credentials for.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "system": {"type": "string"},
                "collection": {"type": "string"},
                "id": {"type": "string"},
            },
            "required": ["system", "collection", "id"],
        },
    },
    {
        "name": "api_write",
        "description": (
            "Create, update or delete one record in an API system. OpenAdapt sends it once "
            "(a retry returns the first result), reads the record back, and checks the "
            "system's own log for other changes by the same credentials."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "system": {"type": "string"},
                "collection": {"type": "string"},
                "op": {"type": "string", "enum": ["create", "update", "delete"]},
                "id": {"type": "string"},
                "slot": {
                    "type": "string",
                    "description": "Names a second create, or a second write to the same record, in this task.",
                },
                "fields": {"type": "object"},
            },
            "required": ["system", "collection", "op"],
        },
    },
]


class ApiTools:
    """The API tools, for one task, in the shape the proxy's MCP server calls."""

    def __init__(self, ctx: Ctx, *, task: str) -> None:
        self.ctx = ctx
        self.task = task

    def tools(self) -> list[dict[str, Any]]:
        return list(TOOLS)

    def call(self, name: str, args: Mapping[str, Any]) -> tuple[str, bool]:
        try:
            if name == "api_read":
                got: Any = self.ctx.adapter(str(args["system"])).read(
                    str(args["collection"]), str(args["id"])
                )
            elif name == "api_write":
                got = scoped_write(
                    self.ctx,
                    task=self.task,
                    system=str(args["system"]),
                    collection=str(args["collection"]),
                    op=str(args["op"]),
                    fields=args.get("fields") or {},
                    rid=str(args["id"]) if args.get("id") not in (None, "") else None,
                    slot=str(args["slot"]) if args.get("slot") else None,
                )
            else:
                return f"unknown tool {name}", True
        except TxRefused as exc:
            return json.dumps({"refused": exc.code, "detail": exc.detail}), True
        except (AdapterError, AuditError, TxSpecError, KeyError, ValueError) as exc:
            return json.dumps(
                {"error": type(exc).__name__, "detail": str(exc)[:300]}
            ), True
        if isinstance(got, Mapping) and got.get("outcome") not in (None, "VERIFIED"):
            return json.dumps(got, default=str), True
        return json.dumps(got, default=str), False
