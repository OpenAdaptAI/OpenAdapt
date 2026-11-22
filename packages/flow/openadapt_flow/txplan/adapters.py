"""Read, list, write and compensate, one adapter per kind of system.

Every adapter speaks in *records*: plain dicts keyed by a record id that is a
string. A *frame* is every record a system shows in the scope a plan touches
(a REST collection, every table of a database, a mailbox), keyed by
``"<collection>/<id>"``. A frame diff is what the plan's dry run predicts and
what the read-back after apply and after undo measures.

* :class:`RestAdapter`: JSON over HTTP. No standard dry run exists, so the
  prediction is computed from the baseline and the planned fields (the
  ``computed`` dry run), with declared server-set and derived fields.
* :class:`SqlAdapter`: SQLite, MySQL/MariaDB or PostgreSQL over DB-API. The
  dry run is a **staged transaction**: the planned statements run inside a
  transaction, every table is read before and after inside it, and the
  transaction is rolled back. Triggers and cascades show up in the diff.
* :class:`MailAdapter`: SMTP out, a Mailpit API to read back. A send is
  irreversible, so it waits in the holdback queue; the Message-ID is derived
  from the idempotency key, so a release after a crash finds the message it
  already sent instead of sending a second.
"""

from __future__ import annotations

import base64
import datetime as _dt
import decimal
import hashlib
import importlib
import json
import os
import re
import secrets
import time
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import quote

from openadapt_flow.txplan.spec import (
    Collection,
    MailSystem,
    RestSystem,
    SqlSystem,
    TxSpecError,
)

Record = dict[str, Any]
Frame = dict[str, str]  # "<collection>/<id>" -> digest of the normalized record


class AdapterError(RuntimeError):
    """A read or a write failed. For a write, delivery is unknown."""


class ChangedSinceApply(AdapterError):
    """Undo found a row no longer as the apply left it."""


class Diverged(AdapterError):
    """The statement did something other than the plan showed. Nothing landed."""


class StaleBaseline(AdapterError):
    """The server refused a conditional write: the record changed since it was
    read. Nothing was written."""


#: Fixed reason codes for a write that can't be VERIFIED because something
#: that could have changed data outside what OpenAdapt read back is not ruled
#: out. They never name a table, trigger, schema or database: run reports are
#: world-readable.
UNCONDITIONAL = ("unconditional_write", "precondition_unproven")


def write_gaps(coll: Collection, op: str, enforced: Optional[bool] = None) -> list[str]:
    """Stale-baseline class: why a write that overwrites or removes fields
    outside the intent (a replace update, a delete) can't be VERIFIED.

    Another caller's edit made after the read can be erased and still read
    back like a correct write, unless the server refused a write whose
    version no longer matched. A declared precondition counts only with
    enforcement evidence: a recorded probe in the spec, or ``enforced``, what
    the send itself proved (a probe the adapter ran and the server refused)."""
    if op not in ("update", "delete"):
        return []
    route = getattr(coll, op, None)
    if route is None or (op == "update" and not route.replace):
        return []
    pre = route.precondition
    if pre is None:
        return ["unconditional_write"]
    # Only what the send itself recorded counts: a stored result without the
    # field (older code, an adoption) never trusts today's spec.
    if enforced is True:
        return []
    return ["precondition_unproven"]


def unconditional(coll: Collection, op: str, enforced: Optional[bool] = None) -> bool:
    return bool(write_gaps(coll, op, enforced))


def _dotted(record: Optional[Mapping[str, Any]], path: str) -> Any:
    got: Any = record
    for part in path.split("."):
        if not isinstance(got, Mapping) or part not in got:
            return None
        got = got[part]
    return got


def precondition_for(
    coll: Collection, op: str, record: Optional[Mapping[str, Any]], etag: Any
) -> Optional[dict[str, str]]:
    """The header that makes this write conditional on the version read, or
    None when the route declares no precondition. A declared precondition
    whose version the read did not give is an error, never an unconditional
    write."""
    route = getattr(coll, op, None)
    pre = getattr(route, "precondition", None)
    if pre is None:
        return None
    value = etag if pre.source == "etag" else _dotted(record, str(pre.field))
    if value in (None, "") or isinstance(value, (bool, Mapping, list)):
        raise AdapterError(f"the read of {coll.name} gave no version to send")
    text = pre.format.replace("{value}", str(value))
    if any(c in text for c in "\r\n\x00"):
        raise AdapterError(f"the version of {coll.name} is not a header value")
    return {pre.header: text}


def after_raw(
    diff: Mapping[str, Any],
    frame: Mapping[str, Mapping[str, str]],
    rows: Mapping[str, Any],
) -> dict[str, Any]:
    """The column digests a statement left: each changed row by id, and each
    added row without its primary key (a sequence may give it another id)."""
    added: dict[str, list[dict[str, str]]] = {}
    for rid in diff["added"]:
        table = rid.split("/", 1)[0]
        pk = set((rows.get(rid) or {}).get("pk") or [])
        added.setdefault(table, []).append(
            {c: d for c, d in frame[rid].items() if c not in pk}
        )
    kinds: dict[str, str] = {}
    recent: set[str] = set()
    for rid in [*diff["added"], *diff["changed"]]:
        table = rid.split("/", 1)[0]
        for c, v in ((rows.get(rid) or {}).get("row") or {}).items():
            when = _as_time(v)
            if kinds.get(f"{table}.{c}") != "other":
                kinds[f"{table}.{c}"] = "time" if when is not None else "other"
            if when is not None and abs(time.time() - when) < 600:
                recent.add(f"{table}.{c}")  # "now", to the second: may differ at apply
    return {
        "changed": {rid: dict(frame[rid]) for rid in diff["changed"]},
        "added": added,
        "kinds": kinds,
        "recent_time": sorted(recent),
    }


_TIME_TEXT = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"
)


def _as_time(v: Any) -> Optional[float]:
    """A tagged datetime, or ISO-looking text, as epoch seconds (naive = UTC or
    local, whichever is closer to now)."""
    raw = v.get("v") if isinstance(v, dict) and v.get("$t") == "dt" else v
    if not isinstance(raw, str) or not _TIME_TEXT.match(raw):
        return None
    try:
        t = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    if t.tzinfo is not None:
        return t.timestamp()
    utc = t.replace(tzinfo=_dt.timezone.utc).timestamp()
    local = t.timestamp()
    return min((utc, local), key=lambda x: abs(time.time() - x))


def volatile_columns(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    """``table.column`` values that differ between two staged runs of the same
    statement (``now()``, ``random()``, a time default): not compared."""
    out: set[str] = set(a.get("recent_time", [])) | set(b.get("recent_time", []))
    for rid in set(a["changed"]) | set(b["changed"]):
        x, y = a["changed"].get(rid, {}), b["changed"].get(rid, {})
        t = rid.split("/", 1)[0]
        out |= {f"{t}.{c}" for c in set(x) | set(y) if x.get(c) != y.get(c)}
    for t in set(a["added"]) | set(b["added"]):
        xs, ys = a["added"].get(t, []), b["added"].get(t, [])
        for c in {c for r in [*xs, *ys] for c in r}:
            if sorted(r.get(c, "") for r in xs) != sorted(r.get(c, "") for r in ys):
                out.add(f"{t}.{c}")
    return sorted(out)


def after_image(raw: Mapping[str, Any], volatile: Sequence[str]) -> dict[str, Any]:
    """``after_raw`` without the volatile columns, in a comparable form."""
    skip = set(volatile)
    changed = {
        rid: {
            c: d for c, d in cols.items() if f"{rid.split('/', 1)[0]}.{c}" not in skip
        }
        for rid, cols in sorted(raw["changed"].items())
    }
    added = {
        t: sorted(
            digest({c: d for c, d in r.items() if f"{t}.{c}" not in skip}) for r in rs
        )
        for t, rs in sorted(raw["added"].items())
    }
    return {"changed": changed, "added": added}


def _shape(diff: Mapping[str, Any]) -> tuple[Any, ...]:
    """A diff, comparable across runs: ids added by a sequence can differ, so
    added rows count per table."""
    added: dict[str, int] = {}
    for rid in diff["added"]:
        t = rid.split("/", 1)[0]
        added[t] = added.get(t, 0) + 1
    return (
        sorted(added.items()),
        sorted(diff["removed"]),
        sorted((k, tuple(v)) for k, v in diff["changed"].items()),
    )


# -- values --------------------------------------------------------------------


def tag(value: Any) -> Any:
    """A JSON form of a database value that decodes to the same type."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$t": "bytes", "v": base64.b64encode(bytes(value)).decode()}
    if isinstance(value, decimal.Decimal):
        return {"$t": "dec", "v": str(value)}
    if isinstance(value, _dt.datetime):
        return {"$t": "dt", "v": value.isoformat()}
    if isinstance(value, _dt.date):
        return {"$t": "date", "v": value.isoformat()}
    if isinstance(value, _dt.timedelta):
        return {"$t": "td", "v": value.total_seconds()}
    return value


def untag(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"$t", "v"}:
        kind, raw = value["$t"], value["v"]
        if kind == "bytes":
            return base64.b64decode(raw)
        if kind == "dec":
            return decimal.Decimal(raw)
        if kind == "dt":
            return _dt.datetime.fromisoformat(raw)
        if kind == "date":
            return _dt.date.fromisoformat(raw)
        if kind == "td":
            return _dt.timedelta(seconds=raw)
    return value


def digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def same(a: Any, b: Any) -> bool:
    """Equal as values (``1`` and ``1.0`` are equal; ``"1"`` and ``1`` aren't; a
    datetime equals its ISO text)."""
    a, b = untag(a), untag(b)
    if isinstance(a, (_dt.date, _dt.datetime)) or isinstance(
        b, (_dt.date, _dt.datetime)
    ):

        def text(v: Any) -> str:
            return (
                v.isoformat()
                if isinstance(v, (_dt.date, _dt.datetime))
                else str(v).replace(" ", "T")
            )

        return text(a) == text(b)
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b or a == b and type(a) is type(b)
    if isinstance(a, (int, float, decimal.Decimal)) and isinstance(
        b, (int, float, decimal.Decimal)
    ):
        return decimal.Decimal(str(a)) == decimal.Decimal(str(b))
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(
        b, sort_keys=True, default=str
    )


# -- diffs ---------------------------------------------------------------------


def diff_frames(
    before: Mapping[str, Mapping[str, str]], after: Mapping[str, Mapping[str, str]]
) -> dict[str, Any]:
    """What moved between two frames of column digests: ids added and removed,
    and the changed columns of each id present in both."""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed: dict[str, list[str]] = {}
    for rid in set(before) & set(after):
        a, b = before[rid], after[rid]
        cols = sorted(c for c in set(a) | set(b) if a.get(c) != b.get(c))
        if cols:
            changed[rid] = cols
    return {
        "added": added,
        "removed": removed,
        "changed": dict(sorted(changed.items())),
    }


def column_digests(
    record: Mapping[str, Any], drop: Sequence[str] = ()
) -> dict[str, str]:
    return {k: digest(tag(v)) for k, v in record.items() if k not in drop}


# -- write scope (SQL) ---------------------------------------------------------
#
# The frame reads one schema (PostgreSQL) or one database (MySQL). Row counts
# show the writes this backend made outside it, but not a TRUNCATE (it zeroes
# them), a write a foreign table or dblink makes from another backend, or
# anything a trigger the account can't see does. So a write is VERIFIED only
# when the catalog shows nothing that could write outside the frame. Coverage
# is decided from the catalog, never by reading a function body: a trigger,
# rule, user function, user type or user cast reachable from the frame caps
# the write, unless it is a trigger an operator reviewed (by digest).

#: PostgreSQL's FirstNormalObjectId: every object with a lower oid came with
#: the server (initdb); anything at or above it was created afterwards, in
#: whatever namespace (pg_catalog included).
_PG_USER_OID = 16384
#: Built-in functions that run SQL given as text or act outside the rows the
#: frame reads. Sequences (nextval) are outside the frame by design.
_PG_EFFECT_FUNCS = re.compile(
    r"^(lo_.*|lowrite|loread|set_config|dblink.*|pg_.*|"
    r".*_to_xml.*|.*_to_xmlschema|ts_stat|xmlexists|xpath.*)$"
)
_MYSQL_BUILTINS = frozenset(
    """
    abs acos adddate addtime ascii asin atan atan2 avg bin bit_and bit_count
    bit_length bit_or bit_xor cast ceil ceiling char char_length
    character_length coalesce concat concat_ws conv convert convert_tz cos cot
    count crc32 curdate current_date current_time current_timestamp
    current_user curtime database date date_add date_format date_sub datediff
    day dayname dayofmonth dayofweek dayofyear decimal degrees elt exp
    extract field find_in_set floor format from_days from_unixtime greatest
    group_concat hex hour if ifnull inet_aton inet_ntoa insert instr interval
    isnull json_array json_contains json_extract json_object json_quote
    json_set json_unquote json_valid last_day last_insert_id lcase least
    left length ln locate log log10 log2 lower lpad ltrim makedate maketime
    max md5 microsecond mid min minute mod month monthname now nullif oct
    octet_length ord period_add period_diff pi position pow power quarter
    quote radians rand regexp_like regexp_replace regexp_substr repeat replace
    reverse right round row_count rpad rtrim schema sec_to_time second sha1
    sha2 sign signed sin space sqrt str_to_date strcmp subdate substr
    substring substring_index subtime sum sysdate tan time time_format
    time_to_sec timediff timestamp timestampadd timestampdiff to_days
    to_seconds trim truncate ucase unhex unix_timestamp unsigned upper user
    utc_date utc_time utc_timestamp uuid uuid_short week weekday weekofyear
    year yearweek binary nchar varchar datetime int integer bigint json
    """.split()
)
#: Words that take a parenthesis without being a call.
_SQL_PAREN_WORDS = frozenset(
    """
    values in exists if elseif while and or not when then else select where on
    as case return returns set using into key index primary unique check over
    partition row any all some between like is null default table from join
    update references foreign by with distinct do of to array
    """.split()
)
#: Words after which a name followed by ``(`` is a table, not a call. On
#: PostgreSQL there are none: every name is looked up in ``pg_proc``, which
#: tells a table from a function. MySQL has no lookup, so only ``INTO t (``
#: (a column list) is skipped.
_NAME_WORDS = {"postgresql": frozenset(), "mysql": frozenset({"into"})}
_IDENT = r'(?:"(?:[^"]|"")+"|`[^`]+`|[A-Za-z_][A-Za-z0-9_$]*)'
_STRIP = {
    # PostgreSQL text here is always the server's own deparse (EXPLAIN,
    # pg_get_expr): no comments or dollar quotes, and any identifier with a
    # character outside [a-z0-9_] is double-quoted.
    "postgresql": re.compile(r"[Ee]'(?:[^'\\]|\\.|'')*'|'(?:[^']|'')*'", re.S),
    "mysql": re.compile(r"--[ \t][^\n]*|#[^\n]*|/\*.*?\*/|'(?:[^'\\]|\\.|'')*'", re.S),
}


def _unquote(name: str) -> str:
    if name[:1] == '"':
        return name[1:-1].replace('""', '"')
    if name[:1] == "`":
        return name[1:-1]
    return name.lower()


def sql_tokens(text: str, dialect: str) -> tuple[set[str], set[str], set[str]]:
    """The qualifiers (``x`` in ``x.y``), the called names (``f`` in
    ``f(``), and the bare keywords of a statement or a function body, with
    comments and string literals left out. A quote the strip can't close
    makes the text unparseable: the keyword ``?unparsed`` is returned."""
    body = _STRIP[dialect].sub(" '' ", text)
    words: set[str] = set()
    if body.count("'") % 2:
        words.add("?unparsed")
    quals = {
        _unquote(m.group(1))
        for m in re.finditer(rf"({_IDENT})\s*\.\s*(?:{_IDENT}|\*)", body)
    }
    calls: set[str] = set()
    # The word before a (possibly qualified) name: ``INTO s.t (`` names a
    # table, not a call.
    before, last = "", ""
    for m in re.finditer(rf"({_IDENT})(\s*\()?|\.", body):
        tok = m.group(1)
        if tok is None:
            last = "."
            continue
        name = _unquote(tok)
        if last != ".":
            before = last
        quoted = tok[:1] in ('"', "`")
        if (
            m.group(2)
            and before.lower() not in _NAME_WORDS[dialect]
            and (quoted or name.lower() not in _SQL_PAREN_WORDS)
        ):
            calls.add(name)
        if tok[:1] not in ('"', "`"):
            words.add(tok.lower())
        last = name
    return quals, calls, words


def _pg_named_gaps(cur: Any, texts: Sequence[str]) -> set[str]:
    """Gaps from functions named in server-deparsed text, resolved through
    ``pg_proc``: a user-defined function (any namespace), or a built-in that
    runs SQL given as text or acts outside the frame's rows."""
    calls: set[str] = set()
    gaps: set[str] = set()
    for text in texts:
        _, c, w = sql_tokens(text, "postgresql")
        calls |= c
        if "?unparsed" in w:
            gaps.add("body_unparsed")
    if calls:
        cur.execute(
            "SELECT p.proname, p.oid::bigint FROM pg_proc p WHERE p.proname = ANY(%s)",
            (sorted(calls),),
        )
        for name, oid in cur.fetchall():
            if int(oid) >= _PG_USER_OID:
                gaps.add("user_function")
            elif _PG_EFFECT_FUNCS.match(str(name)):
                gaps.add("system_function_with_effects")
    return gaps


def _plan_strings(node: Any, out: list[str], schemas: set[str]) -> None:
    if isinstance(node, Mapping):
        for k, v in node.items():
            if k == "Schema" and isinstance(v, str):
                schemas.add(v)
            _plan_strings(v, out, schemas)
    elif isinstance(node, list):
        for v in node:
            _plan_strings(v, out, schemas)
    elif isinstance(node, str):
        out.append(node)


#: The frame: every relation in the current schema.
_PG_FRAME = (
    "SELECT c.oid FROM pg_class c WHERE c.relnamespace = "
    "(SELECT oid FROM pg_namespace WHERE nspname = current_schema())"
)
#: What is attached to a frame relation (depends on it in pg_depend).
_PG_ATTACHED = (
    "SELECT d.classid, d.objid FROM pg_depend d "
    f"WHERE d.refclassid = 'pg_class'::regclass AND d.refobjid IN ({_PG_FRAME})"
)


def pg_trigger_digests(cur: Any) -> dict[str, str]:
    """``{trigger name: sha256}`` for every user trigger on a frame relation:
    the digest a pack lists in ``reviewed_triggers``."""
    cur.execute(
        "SELECT t.tgname, pg_get_triggerdef(t.oid, true), "
        "pg_get_functiondef(t.tgfoid) FROM pg_trigger t "
        f"WHERE NOT t.tgisinternal AND t.tgrelid IN ({_PG_FRAME})"
    )
    return {
        str(name): hashlib.sha256(f"{tdef}\n{fdef}".encode()).hexdigest()
        for name, tdef, fdef in cur.fetchall()
    }


def pg_function_digests(cur: Any) -> dict[str, Optional[str]]:
    """``{signature: sha256}`` for every user function in the database (None
    for an aggregate or window function, which has no definition to review).
    A trigger's helpers leave no pg_depend edge, so a reviewed trigger counts
    only while every user function is reviewed too."""
    cur.execute(
        "SELECT p.oid::regprocedure::text, p.prokind, CASE WHEN p.prokind IN "
        "('f', 'p') THEN pg_get_functiondef(p.oid) END FROM pg_proc p "
        f"WHERE p.oid >= {_PG_USER_OID}"
    )
    return {
        f"function {sig}": (
            hashlib.sha256(f"function\n{fdef}".encode()).hexdigest() if fdef else None
        )
        for sig, _kind, fdef in cur.fetchall()
    }


def _pg_fingerprint(cur: Any) -> str:
    """Every user catalog row that could change what a write runs, by oid
    and row version (xmin): any CREATE OR REPLACE, in this transaction or
    committed by another one, gives a new version."""
    cur.execute(
        "SELECT 'p', oid::text, xmin::text FROM pg_proc "
        f"WHERE oid >= {_PG_USER_OID} UNION ALL "
        "SELECT 't', oid::text, xmin::text FROM pg_trigger "
        f"WHERE oid >= {_PG_USER_OID} UNION ALL "
        "SELECT 'r', oid::text, xmin::text FROM pg_rewrite "
        f"WHERE oid >= {_PG_USER_OID} UNION ALL "
        "SELECT 'e', oid::text, xmin::text FROM pg_event_trigger UNION ALL "
        "SELECT 'c', oid::text, xmin::text FROM pg_cast "
        f"WHERE oid >= {_PG_USER_OID} UNION ALL "
        "SELECT 'y', oid::text, xmin::text FROM pg_type "
        f"WHERE oid >= {_PG_USER_OID}"
    )
    return hashlib.sha256(
        json.dumps(sorted(map(list, cur.fetchall())), default=str).encode()
    ).hexdigest()


def _pg_scope_gaps(cur: Any, sql: str, reviewed: Sequence[str]) -> list[str]:
    gaps: set[str] = set()
    cur.execute("SELECT current_schema(), current_schemas(false)::text[]")
    schema, path = cur.fetchone()
    others = [p for p in (path or []) if p != schema]
    if others:
        # Unqualified names could resolve to another schema, unless it holds
        # nothing a name could resolve to.
        cur.execute(
            "SELECT count(*) FROM pg_namespace n WHERE n.nspname = ANY(%s) AND ("
            "EXISTS (SELECT 1 FROM pg_class c WHERE c.relnamespace = n.oid) OR "
            "EXISTS (SELECT 1 FROM pg_proc p WHERE p.pronamespace = n.oid) OR "
            "EXISTS (SELECT 1 FROM pg_type t WHERE t.typnamespace = n.oid))",
            (others,),
        )
        if int(cur.fetchone()[0]):
            gaps.add("search_path_open")
    if not schema:
        gaps.add("search_path_open")
    # Database-wide: anything here can write outside the frame however the
    # frame's own objects look.
    cur.execute(
        "SELECT (SELECT count(*) FROM pg_foreign_data_wrapper)"
        " + (SELECT count(*) FROM pg_foreign_server)"
        " + (SELECT count(*) FROM pg_foreign_table)"
    )
    if int(cur.fetchone()[0]):
        gaps.add("foreign_data")
    cur.execute(
        "SELECT count(*) FROM pg_extension WHERE extname IN ('dblink', 'postgres_fdw')"
    )
    if int(cur.fetchone()[0]):
        gaps.add("remote_link")
    cur.execute("SELECT count(*) FROM pg_event_trigger WHERE evtenabled <> 'D'")
    if int(cur.fetchone()[0]):
        gaps.add("event_trigger")
    cur.execute(
        f"SELECT (SELECT count(*) FROM pg_cast WHERE oid >= {_PG_USER_OID})"
        f" + (SELECT count(*) FROM pg_operator WHERE oid >= {_PG_USER_OID})"
    )
    if int(cur.fetchone()[0]):
        gaps.add("user_cast_or_operator")
    # Triggers on frame relations: covered only when reviewed, by digest, and
    # only while every user function in the database is reviewed too (a
    # helper a trigger calls leaves no pg_depend edge).
    digests = pg_trigger_digests(cur)
    if set(digests.values()) - set(reviewed):
        gaps.add("trigger")
    elif digests and any(
        d is None or d not in reviewed for d in pg_function_digests(cur).values()
    ):
        gaps.add("trigger_helper_unreviewed")
    # Everything else attached to a frame relation, and what it depends on.
    cur.execute(
        "SELECT a.classid::regclass::text, a.objid::bigint, "
        "CASE WHEN a.classid = 'pg_rewrite'::regclass THEN "
        "(SELECT r.rulename = '_RETURN' FROM pg_rewrite r WHERE r.oid = a.objid) END "
        f"FROM ({_PG_ATTACHED}) a"
    )
    for cls, _oid, is_view in cur.fetchall():
        if cls == "pg_rewrite" and not is_view:
            gaps.add("rule")  # a view's _RETURN rule only runs when read
        elif cls not in (
            "pg_trigger",
            "pg_rewrite",
            "pg_attrdef",
            "pg_constraint",
            "pg_class",
            "pg_type",
            "pg_policy",
            "pg_statistic_ext",
            "pg_publication_rel",
        ):
            gaps.add("user_object")
    # The frame's relations and their attached objects (not triggers, which
    # are covered above, nor views' rules) may depend only on the frame's own
    # relations and types, collations and namespaces. pg_depend lists every
    # object created after initdb; built-ins are pinned and not listed.
    cur.execute(
        "SELECT d.refclassid::regclass::text, d.refobjid::bigint, "
        "CASE WHEN d.refclassid = 'pg_class'::regclass THEN "
        "(SELECT c.relnamespace = (SELECT oid FROM pg_namespace "
        "WHERE nspname = current_schema()) FROM pg_class c WHERE c.oid = d.refobjid) "
        "WHEN d.refclassid = 'pg_type'::regclass THEN "
        "(SELECT t.typtype = 'e' OR t.typrelid IN "
        f"({_PG_FRAME}) FROM pg_type t WHERE t.oid = d.refobjid) END "
        "FROM pg_depend d WHERE ("
        f"(d.classid = 'pg_class'::regclass AND d.objid IN ({_PG_FRAME})) OR "
        f"((d.classid, d.objid) IN (SELECT classid, objid FROM ({_PG_ATTACHED}) a "
        "WHERE a.classid NOT IN ('pg_trigger'::regclass, 'pg_rewrite'::regclass))))"
        f" AND d.refobjid >= {_PG_USER_OID} AND d.deptype <> 'p'"
    )
    for cls, _oid, local in cur.fetchall():
        if cls in ("pg_class", "pg_type"):
            if not local:
                gaps.add("user_type" if cls == "pg_type" else "other_relation")
        elif cls not in ("pg_collation", "pg_namespace", "pg_constraint", "pg_attrdef"):
            gaps.add("user_function" if cls == "pg_proc" else "user_object")
    # Column types made after initdb (domains, extension types), and their
    # constraints and casts: any but an enum caps.
    cur.execute(
        "SELECT count(*) FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid "
        f"WHERE a.attrelid IN ({_PG_FRAME}) AND a.attnum > 0 AND NOT a.attisdropped "
        f"AND t.oid >= {_PG_USER_OID} AND t.typtype <> 'e' "
        f"AND NOT (t.typelem <> 0 AND (SELECT e.typtype FROM pg_type e "
        "WHERE e.oid = t.typelem) = 'e')"
    )
    if int(cur.fetchone()[0]):
        gaps.add("user_type")
    # Built-ins don't appear in pg_depend: read the deparsed expressions of
    # defaults, checks, indexes and policies for ones that run SQL as text.
    cur.execute(
        f"SELECT pg_get_expr(adbin, adrelid) FROM pg_attrdef WHERE adrelid IN ({_PG_FRAME}) "
        "UNION ALL SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        f"WHERE conrelid IN ({_PG_FRAME}) "
        "UNION ALL SELECT pg_get_indexdef(indexrelid) FROM pg_index "
        f"WHERE indrelid IN ({_PG_FRAME}) "
        "UNION ALL SELECT concat(pg_get_expr(polqual, polrelid), ' ', "
        f"pg_get_expr(polwithcheck, polrelid)) FROM pg_policy WHERE polrelid IN ({_PG_FRAME})"
    )
    exprs = [str(r[0] or "") for r in cur.fetchall()]
    gaps |= _pg_named_gaps(cur, exprs)
    if sql:
        if re.match(
            r"^\s*(create|alter|drop|truncate|comment|grant|revoke|rename)\b", sql, re.I
        ):
            # DDL can't be planned: DROP ... CASCADE, TRUNCATE ... CASCADE and
            # event triggers reach past the frame.
            gaps.add(
                "truncate" if re.match(r"^\s*truncate\b", sql, re.I) else "planned_ddl"
            )
        else:
            # The planner can fold a call away (an IMMUTABLE wrapper) or hide
            # one in a cast to a domain, so no plan proves a raw statement
            # free of user code: any user function (other than a trigger or
            # event-trigger function, covered above) or user domain, base or
            # range type in the database caps it.
            cur.execute(
                "SELECT (SELECT count(*) FROM pg_proc "
                f"WHERE oid >= {_PG_USER_OID} AND prorettype NOT IN "
                "('trigger'::regtype, 'event_trigger'::regtype)), "
                f"(SELECT count(*) FROM pg_type WHERE oid >= {_PG_USER_OID} "
                "AND typtype IN ('d', 'b', 'r', 'm') AND typcategory <> 'A')"
            )
            funcs, types = cur.fetchone()
            if int(funcs or 0):
                gaps.add("user_function")
            if int(types or 0):
                gaps.add("user_type")
            # The statement as the server resolves it: views expanded, every
            # relation by its catalog name.
            cur.execute("EXPLAIN (VERBOSE, FORMAT JSON) " + sql)
            raw = cur.fetchone()[0]
            plan = json.loads(raw) if isinstance(raw, str) else raw
            texts: list[str] = []
            schemas: set[str] = set()
            _plan_strings(plan, texts, schemas)
            if schemas - {str(schema), "pg_catalog"}:
                gaps.add("other_schema")
            gaps |= _pg_named_gaps(cur, texts)
    return sorted(gaps)


def _like_literal(pattern: str) -> str:
    """A MySQL grant name as the literal it also matches (``\\_`` is ``_``)."""
    return re.sub(r"\\(.)", r"\1", pattern)


def mysql_grants_visible(grants: Sequence[str], db: str) -> bool:
    """Whether ``SHOW GRANTS`` proves database-level (or global) SELECT and
    TRIGGER, so ``information_schema`` lists every table and trigger in the
    database. Table-level grants don't count: a table the account can't list
    can hold a trigger it can't see.

    Read literally: a wildcard grant counts only for the name it spells, and
    a role grant or a partial revoke proves nothing."""
    if any(str(line).strip().upper().startswith("REVOKE") for line in grants):
        return False  # a partial revoke: a broader grant no longer says enough
    held: set[str] = set()
    for line in grants:
        m = re.match(
            r"^GRANT\s+(.+?)\s+ON\s+(?:TABLE\s+)?(\S+)\s+TO\s",
            str(line).strip(),
            re.I | re.S,
        )
        if not m:
            continue  # a role grant: its privileges are not shown here
        tm = re.fullmatch(r"(\*|`(?:[^`]|``)+`|[A-Za-z0-9_$\\%]+)\.\*", m.group(2))
        if not tm:
            continue  # table- or routine-level
        dbp = tm.group(1)
        if dbp != "*" and _like_literal(dbp.strip("`").replace("``", "`")) != db:
            continue
        privs = re.sub(r"\([^)]*\)", "", m.group(1)).upper()
        names = {p.strip() for p in privs.split(",")}
        if names & {"ALL", "ALL PRIVILEGES"}:
            return True
        held |= names
    return {"SELECT", "TRIGGER"} <= held


def mysql_trigger_digests(cur: Any) -> dict[str, str]:
    """``{trigger name: sha256}`` of every trigger in the database (table,
    timing, event and body): the digest a pack lists in ``reviewed_triggers``."""
    cur.execute(
        "SELECT trigger_name, event_object_table, action_timing, "
        "event_manipulation, action_statement FROM information_schema.triggers "
        "WHERE trigger_schema = DATABASE()"
    )
    return {
        str(row[0]): hashlib.sha256(
            "\n".join(str(x or "") for x in row[1:]).encode()
        ).hexdigest()
        for row in cur.fetchall()
    }


def _mysql_fingerprint(cur: Any) -> str:
    """The database's triggers and routines, as they read now."""
    triggers = mysql_trigger_digests(cur)
    cur.execute(
        "SELECT routine_name, routine_type, last_altered FROM "
        "information_schema.routines WHERE routine_schema = DATABASE()"
    )
    routines = sorted(map(list, cur.fetchall()))
    return hashlib.sha256(
        json.dumps([sorted(triggers.items()), routines], default=str).encode()
    ).hexdigest()


def _mysql_scope_gaps(cur: Any, sql: str, reviewed: Sequence[str]) -> list[str]:
    gaps: set[str] = set()
    cur.execute("SELECT DATABASE()")
    db = str(cur.fetchone()[0] or "")
    cur.execute("SHOW GRANTS")
    grants = [str(r[0]) for r in cur.fetchall()]
    if not db or not mysql_grants_visible(grants, db):
        gaps.add("trigger_privilege_unproven")
    cur.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = DATABASE() "
        "AND UPPER(engine) IN ('FEDERATED', 'CONNECT', 'SPIDER')"
    )
    if int(cur.fetchone()[0]):
        gaps.add("foreign_data")
    # Any trigger in the database (it runs with its definer's rights and can
    # reach tables and databases this account can't see), unless reviewed.
    if set(mysql_trigger_digests(cur).values()) - set(reviewed):
        gaps.add("trigger")
    cur.execute(
        "SELECT count(*) FROM information_schema.routines "
        "WHERE routine_schema = DATABASE()"
    )
    if int(cur.fetchone()[0]):
        gaps.add("routine")
    if sql:
        # No catalog resolution on MySQL: any call that isn't a known
        # built-in caps the statement.
        _, calls, _ = sql_tokens(sql, "mysql")
        if {c for c in calls if c.lower() not in _MYSQL_BUILTINS}:
            gaps.add("user_function")
    return sorted(gaps)


# -- REST ----------------------------------------------------------------------


class RestAdapter:
    kind = "rest"
    dry_run = "computed"

    def __init__(self, system: RestSystem, client: Any = None) -> None:
        import httpx

        self.system = system
        headers = {"Accept": "application/json"}
        if system.auth is not None:
            secret = os.environ.get(system.auth.value_env, "")
            if not secret:
                raise TxSpecError(
                    f"system {system.name}: ${system.auth.value_env} is not set"
                )
            headers[system.auth.header] = system.auth.prefix + secret
        self.http = client or httpx.Client(
            base_url=system.url(), headers=headers, timeout=system.timeout_s
        )
        #: (collection, op) -> whether a probe proved the server enforces the
        #: route's precondition. Probed once per route per adapter.
        self._probed: dict[tuple[str, str], bool] = {}

    @staticmethod
    def _fill(path: str, values: Mapping[str, Any]) -> str:
        def one(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in values or values[name] in (None, ""):
                raise AdapterError(f"the path needs {name}")
            # Path-injection class: a value is one path segment, never a
            # separator, dot segment or query.
            text = str(values[name])
            if text in (".", ".."):
                raise AdapterError(f"{name} can't be a dot segment")
            return quote(text, safe="")

        return re.sub(r"\{([a-z_][a-z0-9_]*)\}", one, path)

    def _raw(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        try:
            return self.http.request(
                method, path, json=body, params=params, headers=dict(headers or {})
            )
        except Exception as exc:  # noqa: BLE001 - transport failure: delivery unknown
            raise AdapterError(f"{method} failed: {type(exc).__name__}") from exc

    def _call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        params: Optional[Mapping[str, Any]] = None,
        key: Optional[str] = None,
        condition: Optional[Mapping[str, str]] = None,
        refused: Sequence[int] = (),
        etag_out: Optional[list[Any]] = None,
    ) -> Any:
        headers = dict(condition or {})
        if key and self.system.idempotency_header:
            headers[self.system.idempotency_header] = key
        resp = self._raw(method, path, body, params=params, headers=headers)
        return self._answer(method, resp, bool(condition), refused, etag_out)

    @staticmethod
    def _answer(
        method: str,
        resp: Any,
        conditional: bool,
        refused: Sequence[int] = (),
        etag_out: Optional[list[Any]] = None,
    ) -> Any:
        if conditional and resp.status_code in refused:
            raise StaleBaseline(
                f"{method} refused: the record changed since it was read"
            )
        if etag_out is not None:
            etag_out.append(resp.headers.get("ETag"))
        if resp.status_code == 404 and method == "GET":
            return None
        if resp.status_code >= 400:
            raise AdapterError(f"{method} returned {resp.status_code}")
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    def enforced(self, coll: Collection, op: str) -> bool:
        """Whether the route's precondition has enforcement evidence: a
        recorded probe, or one this adapter ran."""
        pre = getattr(getattr(coll, op, None), "precondition", None)
        return pre is not None and (
            pre.recorded() or self._probed.get((coll.name, op)) is True
        )

    def probe(
        self, coll: Collection, op: str, send: Callable[[dict[str, str]], Any]
    ) -> tuple[str, Any]:
        """Prove the server enforces the route's precondition, once per route.

        ``send`` makes the planned request with the headers it is given (and
        no idempotency key, so a cached refusal can't answer the real send)
        and returns the response. The probe sends it with a version that
        can't match. ``proven``: the server refused it. ``unproven``: there is
        no evidence, or the probe got another error, and nothing landed.
        ``landed``: the server applied the write without checking the
        version; the response is returned, and the write must not be sent
        again."""
        pre = getattr(getattr(coll, op, None), "precondition", None)
        if pre is None:
            return "unproven", None
        k = (coll.name, op)
        if pre.recorded() or self._probed.get(k) is True:
            return "proven", None
        if not pre.probe or k in self._probed:
            return "unproven", None
        wrong = pre.format.replace("{value}", "openadapt-probe-" + secrets.token_hex(8))
        resp = send({pre.header: wrong})
        if resp.status_code in pre.refused_status:
            self._probed[k] = True
            return "proven", None
        self._probed[k] = False
        if 200 <= resp.status_code < 300:
            return "landed", resp
        return "unproven", None

    def not_covered(
        self, write: Mapping[str, Any], result: Optional[Mapping[str, Any]]
    ) -> list[str]:
        """Why this write's read-back can't make it VERIFIED (empty: nothing)."""
        if write.get("op") not in ("update", "delete"):
            return []
        enforced = (result or {}).get("precondition_enforced")
        return write_gaps(
            self.coll(write.get("collection")),
            write["op"],
            enforced if isinstance(enforced, bool) else None,
        )

    def coll(self, name: Optional[str]) -> Collection:
        return self.system.collection(name)

    def normalize(self, coll: Collection, record: Mapping[str, Any]) -> Record:
        return {k: v for k, v in record.items() if k not in coll.server_fields}

    def read(self, collection: Optional[str], rid: str) -> Optional[Record]:
        return self.read_versioned(collection, rid)[0]

    def read_versioned(
        self, collection: Optional[str], rid: str
    ) -> tuple[Optional[Record], Any]:
        """The record and the ``ETag`` of the same response."""
        coll = self.coll(collection)
        etag: list[Any] = []
        got = self._call(
            coll.read.method, self._fill(coll.read.path, {"id": rid}), etag_out=etag
        )
        if got is None:
            return None, None
        if not isinstance(got, dict):
            raise AdapterError("a read did not return an object")
        return got, (etag[0] if etag else None)

    def condition(
        self, write: Mapping[str, Any], baseline: Optional[Record], etag: Any
    ) -> Optional[dict[str, str]]:
        """The precondition header for a planned write, from its baseline read."""
        if write.get("op") not in ("update", "delete"):
            return None
        return precondition_for(
            self.coll(write.get("collection")), write["op"], baseline, etag
        )

    def frame(self, collection: Optional[str]) -> dict[str, dict[str, str]]:
        coll = self.coll(collection)
        route = coll.list_
        out: dict[str, dict[str, str]] = {}
        page = 1
        while True:
            params: dict[str, Any] = {}
            if route.page_param:
                params[route.page_param] = page
                if route.per_page_param:
                    params[route.per_page_param] = route.per_page
            got = self._call(route.method, route.path, params=params)
            # Missing-evidence class: a missing or malformed list is an error,
            # never an empty collection.
            if isinstance(got, list):
                items = got
            elif isinstance(got, Mapping) and isinstance(got.get("items"), list):
                items = got["items"]
            else:
                raise AdapterError("a list did not return a list")
            fresh = 0
            page_ids: set[str] = set()
            for item in items:
                ident = item.get(coll.id_field) if isinstance(item, Mapping) else None
                if ident in (None, ""):
                    # Missing-evidence class: a row without its identity can't
                    # be placed in the frame.
                    raise AdapterError(f"a {coll.name} row has no {coll.id_field}")
                rid = f"{coll.name}/{ident}"
                if rid in page_ids or rid in out:
                    # One identity per row across the whole scan: a repeat
                    # means the pages shifted, so the frame can't be trusted.
                    raise AdapterError(f"the list repeated {rid}")
                page_ids.add(rid)
                if rid not in out:
                    fresh += 1
                out[rid] = column_digests(self.normalize(coll, item))
            if items and not fresh:
                # A page that repeats rows already read means the pager isn't
                # advancing: the scan can't prove it saw the whole collection.
                raise AdapterError("the list repeated a page")
            if not route.page_param or len(items) < route.per_page:
                return out
            page += 1
            if page > 10_000:
                raise AdapterError("the list never ended")

    def predict(
        self, write: Mapping[str, Any], baseline: Optional[Record]
    ) -> dict[str, Any]:
        coll = self.coll(write.get("collection"))
        fields = dict(write.get("fields") or {})
        op = write["op"]
        if op == "create":
            return {
                "added": {coll.name: 1},
                "changed": {},
                "removed": [],
                "derived": {},
            }
        rid = f"{coll.name}/{write['id']}"
        if op == "delete":
            return {"added": {}, "changed": {}, "removed": [rid], "derived": {}}
        base = baseline or {}
        cols = sorted(
            k
            for k, v in fields.items()
            if k not in coll.server_fields and not same(base.get(k), v)
        )
        derived = sorted({d for c in cols for d in coll.derived.get(c, [])} - set(cols))
        return {
            "added": {},
            "changed": {rid: cols} if cols else {},
            "removed": [],
            "derived": {rid: derived} if derived else {},
        }

    def _body(
        self,
        coll: Collection,
        route: Any,
        base: Optional[Record],
        fields: Mapping[str, Any],
    ) -> Record:
        if route.replace and base is not None:
            body = {k: v for k, v in base.items() if k not in coll.server_fields}
            body.update(fields)
            return body
        return dict(fields)

    def apply(
        self, write: Mapping[str, Any], baseline: Optional[Record], key: str
    ) -> dict[str, Any]:
        coll = self.coll(write.get("collection"))
        op = write["op"]
        fields = dict(write.get("fields") or {})
        route = getattr(coll, op, None)
        if route is None:
            raise AdapterError(f"{coll.name} declares no {op}")
        values = {**(baseline or {}), **fields, "id": write.get("id")}
        condition = write.get("if_match")
        if route.precondition is not None and op != "create" and not condition:
            # Stale-baseline class: a declared precondition is never dropped.
            raise AdapterError("the plan holds no version for the precondition")
        path = self._fill(route.path, values)
        body = None if op == "delete" else self._body(coll, route, baseline, fields)
        if op == "create":
            got = self._call(route.method, path, body, key=key)
            rid = (got or {}).get(route.id_from or coll.id_field)
            if rid in (None, ""):
                raise AdapterError("the create returned no id")
            return {"id": str(rid)}
        out: dict[str, Any] = {"id": str(write["id"])}
        if route.precondition is None:
            self._call(route.method, path, body, key=key)
            return out
        refused = route.precondition.refused_status
        proof, _ = self.probe(
            coll, op, lambda h: self._raw(route.method, path, body, headers=h)
        )
        if proof == "landed":
            # The server applied the probe: the write is in, unchecked.
            out["precondition_enforced"] = False
            return out
        self._call(
            route.method, path, body, key=key, condition=condition, refused=refused
        )
        out["precondition_enforced"] = proof == "proven"
        return out

    def compensate(
        self,
        write: Mapping[str, Any],
        baseline: Optional[Record],
        result: Mapping[str, Any],
        key: str,
        *,
        expect: Optional[Callable[[Record], bool]] = None,
    ) -> list[str]:
        """Undo one write. Returns why the undo can't be VERIFIED (empty when
        it was sent with an enforced precondition).

        Stale-baseline class: the record is read once, with its version;
        ``expect`` checks that read is still what the apply left, and the
        undo carries that version, so an edit landing after the check is
        refused by the server (:class:`StaleBaseline`), never overwritten."""
        coll = self.coll(write.get("collection"))
        op = write["op"]
        rid = str(result.get("id") or write.get("id"))
        if op == "create":
            undo_op, route = "delete", coll.delete
            if route is None:
                raise AdapterError(
                    f"{coll.name} declares no delete, so a create can't be undone"
                )
        elif op == "update":
            undo_op, route = "update", coll.update
            assert route is not None and baseline is not None
        else:
            raise AdapterError(f"no exact undo for a REST {op}")
        current, etag = self.read_versioned(coll.name, rid)
        if current is None or (expect is not None and not expect(current)):
            raise ChangedSinceApply(f"{coll.name}/{rid}")
        condition = precondition_for(coll, undo_op, current, etag)
        if undo_op == "delete":
            path, body = self._fill(route.path, {"id": rid}), None
        else:
            assert baseline is not None
            changed = list(write.get("fields") or {})
            body = (
                {k: v for k, v in baseline.items() if k not in coll.server_fields}
                if route.replace
                else {k: baseline.get(k) for k in changed}
            )
            path = self._fill(route.path, {**baseline, "id": rid})
        if condition is None:
            self._call(route.method, path, body, key=key + ":undo")
            return ["unconditional_undo"]
        proof, _ = self.probe(
            coll, undo_op, lambda h: self._raw(route.method, path, body, headers=h)
        )
        if proof == "landed":
            return ["unconditional_undo"]
        self._call(
            route.method,
            path,
            body,
            key=key + ":undo",
            condition=condition,
            refused=route.precondition.refused_status if route.precondition else (),
        )
        return [] if proof == "proven" else ["unconditional_undo"]

    def expected_after(
        self, write: Mapping[str, Any], baseline: Optional[Record]
    ) -> Optional[dict[str, Any]]:
        """The planned fields, for the record read-back after apply."""
        if write["op"] == "delete":
            return None
        coll = self.coll(write.get("collection"))
        return {
            k: v
            for k, v in (write.get("fields") or {}).items()
            if k not in coll.server_fields
        }

    def frame_id(self, write: Mapping[str, Any], result: Mapping[str, Any]) -> str:
        return f"{write.get('collection')}/{result.get('id') or write.get('id')}"


# -- SQL -----------------------------------------------------------------------


class SqlAdapter:
    kind = "sql"
    dry_run = "staged_transaction"

    def __init__(self, system: SqlSystem, root: Any = None) -> None:
        self.system = system
        self.root = root
        self.ph = "?" if system.dialect == "sqlite" else "%s"
        self.q = "`" if system.dialect == "mysql" else '"'

    # connection -------------------------------------------------------------

    def connect(self, *, read_only: bool = False) -> Any:
        s = self.system
        if s.dialect == "sqlite":
            import sqlite3
            from pathlib import Path

            path = Path(str(s.path))
            if not path.is_absolute() and self.root is not None:
                path = Path(self.root) / path
            if read_only:
                uri = path.resolve().as_uri() + "?mode=ro"
                return sqlite3.connect(uri, uri=True, isolation_level=None)
            con = sqlite3.connect(str(path), isolation_level=None)
            # Unenforced-constraint class: a parent delete must not leave
            # orphans that the frame read still calls clean.
            con.execute("PRAGMA foreign_keys = ON")
            return con
        module = importlib.import_module(str(s.driver))
        args = dict(s.connect_args)
        if s.password_env:
            pw = os.environ.get(s.password_env)
            if pw is None:
                raise TxSpecError(f"system {s.name}: ${s.password_env} is not set")
            args["password"] = pw
        con = module.connect(**args)
        if hasattr(con, "autocommit") and callable(getattr(con, "autocommit")):
            con.autocommit(False)  # pymysql
        return con

    def read_mapping(self) -> dict[str, Any]:
        """The read connection in ``frame.database.read`` form, for the verifier."""
        s = self.system
        if s.dialect == "sqlite":
            from pathlib import Path

            path = Path(str(s.path))
            if not path.is_absolute() and self.root is not None:
                path = Path(self.root) / path
            return {"sqlite_database": str(path)}
        return {
            "sql_driver": s.driver,
            "sql_connect_args": dict(s.connect_args),
            **({"sql_password_env": s.password_env} if s.password_env else {}),
        }

    def _begin(self, cur: Any) -> None:
        if self.system.dialect == "postgresql":
            return  # the DB-API driver opens a transaction on the first statement
        cur.execute("BEGIN" if self.system.dialect != "mysql" else "START TRANSACTION")

    def _ident(self, name: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,63}", name):
            raise AdapterError(f"bad identifier {name!r}")
        return f"{self.q}{name}{self.q}"

    # reading ----------------------------------------------------------------

    def _refuse_unobserved_writes(self, cur: Any, sql: str) -> None:
        """Unobserved-scope class: the frame reads one schema (or database), so
        a statement may not change rows anywhere else. This refuses writes
        that are seen; :meth:`scope_gaps` caps what can't be seen."""
        d = self.system.dialect
        if d == "postgresql":
            # This transaction's own row counts, for every table it touched.
            cur.execute(
                "SELECT count(*) FROM pg_stat_xact_user_tables "
                "WHERE schemaname <> current_schema() "
                "AND n_tup_ins + n_tup_upd + n_tup_del > 0"
            )
            if int(cur.fetchone()[0]):
                raise Diverged("the statement changed rows outside the current schema")
        elif d == "mysql" and sql:
            cur.execute("SELECT DATABASE()")
            current = str(cur.fetchone()[0] or "")
            cur.execute("SHOW DATABASES")
            others = {str(r[0]).lower() for r in cur.fetchall()} - {current.lower()}
            named = {q.lower() for q in sql_tokens(sql, "mysql")[0]}
            if named & others:
                raise Diverged("the statement names another database")

    def _scope_read(self, cur: Any, sql: str) -> tuple[list[str], Optional[str]]:
        """The inventory's gaps and a fingerprint of the user catalog rows."""
        d = self.system.dialect
        if d == "sqlite":
            # One file: ATTACH is refused, so a trigger can only write tables
            # the frame reads, and the connection registers no functions.
            return [], ""
        if d == "postgresql":
            # A failed catalog read aborts a PostgreSQL transaction: read under
            # a savepoint, so an unreadable inventory caps the write instead of
            # refusing it.
            cur.execute("SAVEPOINT oa_scope")
            try:
                gaps = _pg_scope_gaps(cur, sql, self.system.reviewed_triggers)
                fp = _pg_fingerprint(cur)
            except Exception:  # noqa: BLE001 - unreadable: capped, not refused
                cur.execute("ROLLBACK TO SAVEPOINT oa_scope")
                return ["scope_unreadable"], None
            cur.execute("RELEASE SAVEPOINT oa_scope")
            return gaps, fp
        try:
            return (
                _mysql_scope_gaps(cur, sql, self.system.reviewed_triggers),
                _mysql_fingerprint(cur),
            )
        except Exception:  # noqa: BLE001 - unreadable: capped, not refused
            return ["scope_unreadable"], None

    def scope_before(self, cur: Any, sql: str = "") -> tuple[list[str], Optional[str]]:
        """The inventory before the write, in its transaction. On PostgreSQL it
        also tries to hold the user functions still (``FOR SHARE`` on their
        pg_proc rows, which needs a privileged account); the comparison after
        the write is what counts either way."""
        if self.system.dialect == "postgresql":
            cur.execute("SAVEPOINT oa_lock")
            try:
                cur.execute(
                    f"SELECT oid FROM pg_proc WHERE oid >= {_PG_USER_OID} FOR SHARE"
                )
                cur.execute("RELEASE SAVEPOINT oa_lock")
            except Exception:  # noqa: BLE001 - not allowed: compare instead
                cur.execute("ROLLBACK TO SAVEPOINT oa_lock")
        return self._scope_read(cur, sql)

    def scope_gaps(
        self,
        cur: Any,
        sql: str = "",
        before: Optional[tuple[list[str], Optional[str]]] = None,
    ) -> list[str]:
        """Closed write-scope check, inside the write's own transaction.

        A database write may be VERIFIED only when the catalog shows nothing
        that could write outside the observed frame. Returns fixed reason
        codes, one per open item; an inventory that can't be read is
        ``scope_unreadable``. With ``before`` (read before the write, in the
        same transaction), the gaps of both reads count, and any change to the
        user catalog between them (a function replaced while the write ran,
        by it or by another session) is ``scope_changed``. A gap never refuses
        the write: it caps the outcome at ``COMPLETED_UNVERIFIED``."""
        gaps, fp = self._scope_read(cur, sql)
        if before is None:
            return gaps
        bgaps, bfp = before
        out = set(gaps) | set(bgaps)
        if fp is not None and bfp is not None and fp != bfp:
            out.add("scope_changed")
        return sorted(out)

    def trigger_digests(self) -> dict[str, str]:
        """The digests an operator reviews and lists in ``reviewed_triggers``:
        each trigger, and on PostgreSQL every user function in the database
        (a reviewed trigger counts only while all of them are listed)."""
        con = self.connect()
        try:
            cur = con.cursor()
            self._begin(cur)
            try:
                if self.system.dialect == "postgresql":
                    out = {
                        f"trigger {k}": v for k, v in pg_trigger_digests(cur).items()
                    }
                    for sig, d in pg_function_digests(cur).items():
                        if d is not None:
                            out[sig] = d
                    return out
                if self.system.dialect == "mysql":
                    return mysql_trigger_digests(cur)
                return {}
            finally:
                con.rollback()
        finally:
            con.close()

    def not_covered(
        self, write: Mapping[str, Any], result: Optional[Mapping[str, Any]]
    ) -> list[str]:
        """The scope gaps recorded with the write. A result without them (an
        adoption after a crash, a ledger entry from older code) is never
        VERIFIED: nothing says what could fire then."""
        gaps = (result or {}).get("not_covered")
        if not isinstance(gaps, list):
            # SQLite has nothing to list (see scope_gaps), so an adoption or
            # an older entry there loses nothing.
            return [] if self.system.dialect == "sqlite" else ["scope_unrecorded"]
        return [str(g) for g in gaps]

    def _tables(self, cur: Any) -> list[str]:
        d = self.system.dialect
        if d == "sqlite":
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        elif d == "mysql":
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'"
            )
        else:
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            )
        names = sorted(str(r[0]) for r in cur.fetchall())
        return [n for n in names if n not in self.system.frame_ignore_tables]

    def _pk(self, cur: Any, table: str) -> list[str]:
        if table in self.system.tables:
            return [self.system.tables[table]]
        d = self.system.dialect
        if d == "sqlite":
            cur.execute(f"PRAGMA table_info({self._ident(table)})")
            cols = sorted((r[5], r[1]) for r in cur.fetchall() if r[5])
            return [c for _, c in cols]
        if d == "mysql":
            cur.execute(
                "SELECT column_name FROM information_schema.key_column_usage WHERE "
                "table_schema = DATABASE() AND table_name = %s AND constraint_name = 'PRIMARY' "
                "ORDER BY ordinal_position",
                (table,),
            )
        else:
            cur.execute(
                "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
                "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = %s::regclass AND i.indisprimary",
                (table,),
            )
        return [str(r[0]) for r in cur.fetchall()]

    def _ignored(self, table: str, col: str) -> bool:
        rules = self.system.frame_ignore_columns
        return f"{table}.{col}" in rules or f"*.{col}" in rules

    def _rows(self, cur: Any, table: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        cur.execute(f"SELECT * FROM {self._ident(table)}")
        cols = [str(d[0]) for d in cur.description]
        return cols, list(cur.fetchall())

    def frame_cur(
        self, cur: Any, rows_out: Optional[dict[str, Any]] = None
    ) -> dict[str, dict[str, str]]:
        """Every table's rows as column digests. With ``rows_out``, also keep
        each row's tagged values there (in memory, for undo images)."""
        out: dict[str, dict[str, str]] = {}
        total = 0
        for table in self._tables(cur):
            pk = self._pk(cur, table)
            cols, rows = self._rows(cur, table)
            total += len(rows)
            if total > self.system.max_frame_rows:
                raise AdapterError("the database has more rows than max_frame_rows")
            seen: dict[str, int] = {}
            for row in rows:
                rec = dict(zip(cols, row))
                digs = {
                    c: digest(tag(v))
                    for c, v in rec.items()
                    if not self._ignored(table, c)
                }
                if pk and all(c in rec for c in pk):
                    rid = "|".join(str(rec[c]) for c in pk)
                else:  # no key: the whole row is its identity
                    base = digest(digs)
                    seen[base] = seen.get(base, 0) + 1
                    rid = f"~{base}#{seen[base]}"
                out[f"{table}/{rid}"] = digs
                if rows_out is not None:
                    rows_out[f"{table}/{rid}"] = {
                        "pk": pk,
                        "row": {c: tag(v) for c, v in rec.items()},
                    }
        return out

    def frame(self, collection: Optional[str] = None) -> dict[str, dict[str, str]]:
        con = self.connect()
        try:
            cur = con.cursor()
            self._begin(cur)
            try:
                return self.frame_cur(cur)
            finally:
                con.rollback()
        finally:
            con.close()

    def _read_cur(self, cur: Any, table: str, rid: str) -> Optional[Record]:
        key = self.system.tables.get(table)
        if key is None:
            raise AdapterError(f"table {table} is not declared")
        cur.execute(
            f"SELECT * FROM {self._ident(table)} WHERE {self._ident(key)} = {self.ph}",
            (rid,),
        )
        cols = [str(d[0]) for d in cur.description]
        rows = cur.fetchall()
        if len(rows) > 1:
            raise AdapterError(f"{table}.{key} is not unique")
        return {c: tag(v) for c, v in zip(cols, rows[0])} if rows else None

    def read(self, collection: Optional[str], rid: str) -> Optional[Record]:
        con = self.connect()
        try:
            cur = con.cursor()
            self._begin(cur)
            try:
                return self._read_cur(cur, str(collection), rid)
            finally:
                con.rollback()
        finally:
            con.close()

    def normalize(self, coll: Any, record: Mapping[str, Any]) -> Record:
        return dict(record)

    # writing ----------------------------------------------------------------

    def _exec(
        self, cur: Any, write: Mapping[str, Any], baseline: Optional[Record]
    ) -> dict[str, Any]:
        sql = (
            str((write.get("fields") or {}).get("sql", ""))
            if write["op"] == "sql"
            else ""
        )
        # The inventory before the write as well as after: a trigger that
        # rewrites itself, or a function replaced by another session while the
        # write runs, shows as a difference.
        before = self.scope_before(cur, sql)
        result = self._exec_one(cur, write, baseline)
        if self.system.dialect == "postgresql":
            # Unobserved-scope class: a deferred constraint trigger fires at
            # COMMIT, after every check and frame read. Fire it now, inside
            # the transaction, where the scope check and the frame see it.
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        # Every op, not only raw SQL: a trigger on a planned insert, update or
        # delete can write outside the frame too.
        self._refuse_unobserved_writes(cur, sql)
        result["not_covered"] = self.scope_gaps(cur, sql, before)
        return result

    def _exec_one(
        self, cur: Any, write: Mapping[str, Any], baseline: Optional[Record]
    ) -> dict[str, Any]:
        table = str(write["collection"])
        key = self.system.tables.get(table)
        if key is None and write["op"] != "sql":
            raise AdapterError(f"table {table} is not declared")
        op = write["op"]
        if op == "sql":
            cur.execute(str((write.get("fields") or {})["sql"]))
            rows = max(0, int(cur.rowcount or 0))
            return {"rows": rows}
        fields = {k: untag(v) for k, v in (write.get("fields") or {}).items()}
        assert key is not None
        t = self._ident(table)
        if op == "create":
            cols = list(fields)
            sql = (
                f"INSERT INTO {t} ({', '.join(self._ident(c) for c in cols)}) "
                f"VALUES ({', '.join([self.ph] * len(cols))})"
            )
            if self.system.dialect == "postgresql":
                sql += f" RETURNING {self._ident(key)}"
            cur.execute(sql, tuple(fields[c] for c in cols))
            if self.system.dialect == "postgresql":
                rid = cur.fetchone()[0]
            else:
                rid = fields.get(key, cur.lastrowid)
            if cur.rowcount != 1:
                raise AdapterError("the insert did not add one row")
            return {"id": str(rid)}
        rid = str(write["id"])
        if op == "update":
            sets = ", ".join(f"{self._ident(c)} = {self.ph}" for c in fields)
            cur.execute(
                f"UPDATE {t} SET {sets} WHERE {self._ident(key)} = {self.ph}",
                (*fields.values(), rid),
            )
        elif op == "delete":
            cur.execute(f"DELETE FROM {t} WHERE {self._ident(key)} = {self.ph}", (rid,))
        else:
            raise AdapterError(f"no SQL {op}")
        if cur.rowcount != 1 and not (
            op == "update" and self.system.dialect == "mysql"
        ):
            raise AdapterError(f"the {op} touched {cur.rowcount} rows, not one")
        return {"id": rid}

    def stage(
        self, writes: Sequence[Mapping[str, Any]], baselines: Sequence[Optional[Record]]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Run the writes inside a transaction, read every table before and
        after inside it, and roll back. Returns the frame diff and each
        write's staged result."""
        con = self.connect()
        try:
            cur = con.cursor()
            self._begin(cur)
            try:
                rows: dict[str, Any] = {}
                before = self.frame_cur(cur, rows)
                results = []
                prev = before
                for w, b in zip(writes, baselines):
                    result = self._exec(cur, w, b)
                    if w["op"] == "sql":
                        now_rows: dict[str, Any] = {}
                        now_ = self.frame_cur(cur, now_rows)
                        result["staged_diff"] = diff_frames(prev, now_)
                        result["after_raw"] = after_raw(
                            result["staged_diff"], now_, now_rows
                        )
                        result["before_rows"] = {
                            rid: rows[rid]["row"]
                            for rid in [
                                *result["staged_diff"]["removed"],
                                *result["staged_diff"]["changed"],
                            ]
                            if rid in rows
                        }
                        prev = now_
                    results.append(result)
                after = self.frame_cur(cur)
            finally:
                con.rollback()
        finally:
            con.close()
        return diff_frames(before, after), results

    def predict(
        self, write: Mapping[str, Any], baseline: Optional[Record]
    ) -> dict[str, Any]:
        raise NotImplementedError("SQL plans are staged as one transaction")

    def apply(
        self, write: Mapping[str, Any], baseline: Optional[Record], key: str
    ) -> dict[str, Any]:
        if write["op"] == "sql":
            return self._apply_sql(write)
        con = self.connect()
        try:
            cur = con.cursor()
            self._begin(cur)
            try:
                result = self._exec(cur, write, baseline)
            except Exception:
                con.rollback()
                raise
            con.commit()
            return result
        finally:
            con.close()

    def _apply_sql(self, write: Mapping[str, Any]) -> dict[str, Any]:
        """Run one statement; commit only if its diff, read inside the same
        transaction, is exactly the diff the plan showed. Keep the undo images."""
        want = write.get("staged_diff")
        if not isinstance(want, Mapping):
            raise AdapterError("the plan has no staged diff for this statement")
        # The planned values, not only the shape: without them a statement
        # could commit different values into the planned rows.
        if not isinstance(write.get("staged_after"), Mapping):
            raise AdapterError("the plan has no staged values for this statement")
        con = self.connect()
        try:
            cur = con.cursor()
            self._begin(cur)
            try:
                rows: dict[str, Any] = {}
                before = self.frame_cur(cur, rows)
                result = self._exec(cur, write, None)
                after_rows: dict[str, Any] = {}
                after = self.frame_cur(cur, after_rows)
                diff = diff_frames(before, after)
                got_after = after_image(
                    after_raw(diff, after, after_rows), write.get("volatile") or []
                )
                if _shape(diff) != _shape(want) or got_after != write["staged_after"]:
                    con.rollback()
                    raise Diverged(
                        "the statement's rows or values are not the planned ones; rolled back"
                    )
                images = {
                    "before": {
                        r: rows[r] for r in [*diff["removed"], *diff["changed"]]
                    },
                    "after": {r: after[r] for r in [*diff["added"], *diff["changed"]]},
                    "added_pk": {r: after_rows[r] for r in diff["added"]},
                }
            except Diverged:
                raise
            except Exception:
                con.rollback()
                raise
            con.commit()
        finally:
            con.close()
        # Unverified-success class: what committed is read again, outside the
        # transaction, so a deferred trigger that stored other values in the
        # planned rows is a refuted write, not a confirmed one.
        post_rows: dict[str, Any] = {}
        con = self.connect()
        try:
            cur = con.cursor()
            self._begin(cur)
            try:
                post = self.frame_cur(cur, post_rows)
            finally:
                con.rollback()
        finally:
            con.close()
        post_diff = diff_frames(before, post)
        post_after = after_image(
            after_raw(post_diff, post, post_rows), write.get("volatile") or []
        )
        matches = (
            _shape(post_diff) == _shape(want) and post_after == write["staged_after"]
        )
        return {**result, "post_commit_matches": matches, "_images": images}

    def compensate_sql(self, images: Mapping[str, Any]) -> list[str]:
        """Undo one statement from its images, in one transaction: delete what
        it added, restore what it changed, reinsert what it removed.

        Foreign keys can make the order matter (a cascade removed children
        with their parent), so each row runs under a savepoint and failed rows
        are retried in later passes until none makes progress."""
        steps: list[tuple[str, str, Mapping[str, Any]]] = [
            ("delete", rid, item) for rid, item in images.get("added_pk", {}).items()
        ] + [("restore", rid, item) for rid, item in images.get("before", {}).items()]
        con = self.connect()
        try:
            cur = con.cursor()
            self._begin(cur)
            try:
                before = self.scope_before(cur)
                # Stale-baseline class: the check and the restore share one
                # transaction, so an edit landing between them can't be
                # overwritten.
                now_rows = self.frame_cur(cur)
                after = images.get("after", {})
                for rid, want in after.items():
                    if now_rows.get(rid) != want:
                        raise ChangedSinceApply(rid)
                for rid in images.get("before", {}):
                    if rid not in after and now_rows.get(rid) is not None:
                        raise ChangedSinceApply(rid)
                left = steps
                n = 0
                while left:
                    failed = []
                    for step in left:
                        n += 1
                        sp = f"oa_undo_{n}"
                        cur.execute(f"SAVEPOINT {sp}")
                        try:
                            if step[0] == "delete":
                                self._delete_row(cur, step[1], step[2])
                            else:
                                self._restore_row(cur, step[1], step[2])
                            cur.execute(f"RELEASE SAVEPOINT {sp}")
                        except AdapterError:
                            raise
                        except Exception:  # noqa: BLE001 - a constraint: retry after the others
                            cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                            failed.append(step)
                    if len(failed) == len(left):
                        raise AdapterError(
                            f"{len(failed)} row(s) could not be restored"
                        )
                    left = failed
                if self.system.dialect == "postgresql":
                    cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
                # The undo's own writes fire the same triggers.
                self._refuse_unobserved_writes(cur, "")
                gaps = self.scope_gaps(cur, "", before)
            except Exception:
                con.rollback()
                raise
            con.commit()
        finally:
            con.close()
        return gaps

    def _restore_row(self, cur: Any, rid: str, item: Mapping[str, Any]) -> None:
        table = rid.split("/", 1)[0]
        pk, row = item["pk"], item["row"]
        if not pk:
            raise AdapterError(f"{table} has no primary key: no generated undo")
        where = " AND ".join(f"{self._ident(c)} = {self.ph}" for c in pk)
        cur.execute(
            f"SELECT 1 FROM {self._ident(table)} WHERE {where}",
            tuple(untag(row[c]) for c in pk),
        )
        if cur.fetchall():
            cols = [c for c in row if c not in pk]
            if cols:
                sets = ", ".join(f"{self._ident(c)} = {self.ph}" for c in cols)
                cur.execute(
                    f"UPDATE {self._ident(table)} SET {sets} WHERE {where}",
                    (*(untag(row[c]) for c in cols), *(untag(row[c]) for c in pk)),
                )
        else:
            cols = list(row)
            cur.execute(
                f"INSERT INTO {self._ident(table)} ({', '.join(self._ident(c) for c in cols)}) "
                f"VALUES ({', '.join([self.ph] * len(cols))})",
                tuple(untag(row[c]) for c in cols),
            )

    def _delete_row(self, cur: Any, rid: str, item: Mapping[str, Any]) -> None:
        table = rid.split("/", 1)[0]
        pk, row = item["pk"], item["row"]
        if not pk:
            raise AdapterError(f"{table} has no primary key: no generated undo")
        where = " AND ".join(f"{self._ident(c)} = {self.ph}" for c in pk)
        cur.execute(
            f"DELETE FROM {self._ident(table)} WHERE {where}",
            tuple(untag(row[c]) for c in pk),
        )

    def current_digests(
        self, rids: Sequence[str]
    ) -> dict[str, Optional[dict[str, str]]]:
        """The current column digests of some frame rows (None when gone)."""
        frame = self.frame()
        return {r: frame.get(r) for r in rids}

    def compensate(
        self,
        write: Mapping[str, Any],
        baseline: Optional[Record],
        result: Mapping[str, Any],
        key: str,
        *,
        expect: Optional[Callable[[Record], bool]] = None,
    ) -> list[str]:
        """Undo one write; returns the undo's own scope gaps."""
        op = write["op"]
        rid = str(result.get("id") or write.get("id"))
        if op == "create":
            inverse = {"op": "delete", "collection": write["collection"], "id": rid}
        elif op == "update":
            assert baseline is not None
            # The whole before-image, not just the planned columns: a column the
            # database set itself (ON UPDATE CURRENT_TIMESTAMP, a trigger on the
            # same row) is restored too, by naming its old value explicitly.
            current = self.read(write["collection"], rid) or {}
            fields = {
                k: v
                for k, v in baseline.items()
                if k in current and not same(current.get(k), v)
            } or {k: baseline.get(k) for k in (write.get("fields") or {})}
            inverse = {
                "op": "update",
                "collection": write["collection"],
                "id": rid,
                "fields": fields,
            }
        elif op == "delete":
            assert baseline is not None
            inverse = {
                "op": "create",
                "collection": write["collection"],
                "fields": dict(baseline),
            }
        else:
            raise AdapterError(f"no SQL undo for {op}")
        done = self.apply(inverse, None, key + ":undo")
        return self.not_covered(inverse, done)

    def expected_after(
        self, write: Mapping[str, Any], baseline: Optional[Record]
    ) -> Optional[dict[str, Any]]:
        if write["op"] == "delete":
            return None
        return {k: tag(untag(v)) for k, v in (write.get("fields") or {}).items()}

    def frame_id(self, write: Mapping[str, Any], result: Mapping[str, Any]) -> str:
        return f"{write['collection']}/{result.get('id') or write.get('id')}"


# -- mail ----------------------------------------------------------------------


class MailAdapter:
    kind = "mail"
    dry_run = "holdback"

    def __init__(self, system: MailSystem, client: Any = None) -> None:
        import httpx

        self.system = system
        self.http = client or httpx.Client(base_url=system.mailbox.url(), timeout=20.0)

    @staticmethod
    def message_id(key: str) -> str:
        return f"<{key}@openadapt.tx>"

    def _messages(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        start = 0
        while True:
            resp = self.http.get(
                "/api/v1/messages", params={"start": start, "limit": 250}
            )
            if resp.status_code >= 400:
                raise AdapterError(f"the mailbox returned {resp.status_code}")
            page = resp.json().get("messages") or []
            out += page
            if len(page) < 250:
                return out
            start += len(page)

    def frame(self, collection: Optional[str] = None) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        counts: dict[str, int] = {}
        for m in self._messages():
            mid = str(m.get("MessageID") or m.get("ID"))
            counts[mid] = counts.get(mid, 0) + 1
            out[f"message/{mid}#{counts[mid]}"] = {
                "subject": digest(m.get("Subject")),
                "to": digest(sorted(a.get("Address", "") for a in m.get("To") or [])),
            }
        return out

    def count(self, key: str) -> int:
        mid = self.message_id(key).strip("<>")
        return sum(
            1
            for m in self._messages()
            if str(m.get("MessageID", "")).strip("<>") == mid
        )

    def predict(
        self, write: Mapping[str, Any], baseline: Optional[Record]
    ) -> dict[str, Any]:
        return {"added": {}, "changed": {}, "removed": [], "derived": {}, "queued": 1}

    def send(
        self,
        write: Mapping[str, Any],
        key: str,
        *,
        smtp_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        import smtplib
        from email.message import EmailMessage

        fields = dict(write.get("fields") or {})
        msg = EmailMessage()
        msg["From"] = self.system.sender
        to = fields.get("to")
        msg["To"] = ", ".join(to) if isinstance(to, list) else str(to)
        msg["Subject"] = str(fields.get("subject", ""))
        msg["Message-ID"] = self.message_id(key)
        msg["X-OpenAdapt-Key"] = key
        msg.set_content(str(fields.get("body", "")))
        factory = smtp_factory or smtplib.SMTP
        try:
            with factory(
                self.system.smtp.host, self.system.smtp.port, timeout=20
            ) as smtp:
                smtp.send_message(msg)
        except Exception as exc:  # noqa: BLE001 - delivery unknown
            raise AdapterError(f"SMTP failed: {type(exc).__name__}") from exc

    def wait_count(self, key: str, want: int, timeout_s: float = 10.0) -> int:
        deadline = time.monotonic() + timeout_s
        n = self.count(key)
        while n < want and time.monotonic() < deadline:
            time.sleep(0.2)
            n = self.count(key)
        return n


def adapter_for(system: Any, root: Any = None) -> Any:
    if isinstance(system, RestSystem):
        return RestAdapter(system)
    if isinstance(system, SqlSystem):
        return SqlAdapter(system, root)
    if isinstance(system, MailSystem):
        return MailAdapter(system)
    raise TxSpecError(f"unknown system kind {type(system).__name__}")
