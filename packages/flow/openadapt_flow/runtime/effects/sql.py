"""Read-only SQL system-of-record :class:`EffectVerifier` + table-delta audit.

Promotes the SQL-audit verification the governed Frappe Lending reference
matrix proved (``benchmark/frappe_lending/fixture.py``, merged PR #131) from
bespoke benchmark plumbing into a deployable kit component:

- :class:`SqlRecordVerifier` -- a PARAMETERIZED read-only SELECT is the record
  probe; rows are judged by the shared :func:`judge_records` (at-most-once,
  idempotency key, field read-back, collateral loss, ``count_new_only``),
  exactly like every other substrate.
- :func:`audit_table_deltas` -- the EXACT row-count-delta contract lifted from
  the Frappe fixture: every table named in the contract must move by exactly
  the declared delta, and every OTHER table must move by exactly 0 (a write
  that "worked" but also inserted a stray row elsewhere is a violation).
- :func:`capture_table_counts` -- read-only ``COUNT(*)`` per audited table,
  the before/after inputs to :func:`audit_table_deltas`.

READ-ONLY is enforced in TWO layers, and the statement filter is only the
inner one: :func:`assert_read_only_sql` refuses anything but a single
``SELECT``/``WITH``-``SELECT`` statement (no stacking, no comments, no
mutating/DDL keywords, no known side-effecting functions) AT CONSTRUCTION
TIME, and values are bound through DB-API parameters (never
string-interpolated) so a run param cannot inject SQL. But a keyword filter
CANNOT prove a SELECT is side-effect-free on a real engine -- a Postgres
``SELECT some_udf(...)`` or vendor function can mutate state while passing
every lexical check. The REAL enforcement is the database role: run this
verifier under a dedicated READ-ONLY account (no INSERT/UPDATE/DELETE, no
EXECUTE on writing functions, no sequence privileges), as
``docs/EFFECT_KIT.md`` instructs. The filter then guards against config
mistakes; the role guards against everything.

Driver-neutral: the caller supplies a zero-arg ``connect`` callable returning
a DB-API 2.0 connection (``sqlite3.connect`` in tests and CI; ``pymysql`` /
``mariadb`` / ``psycopg`` in a deployment). The kit imports no driver itself.

Fail-safe: any connect/execute error reads as *unreadable* -> INDETERMINATE ->
HALT, never a guessed success. A fresh connection is opened per read so a
poll never judges a stale transaction snapshot.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Iterable, Mapping, Optional

from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
)

# Keywords that could make a SELECT-leading statement mutate. Matched on word
# boundaries anywhere in the statement (defense in depth beyond the
# leading-keyword + no-stacking checks): a data-modifying CTE
# (``WITH x AS (INSERT ... RETURNING ...)``), ``SELECT ... INTO``, and
# sqlite/vendor escapes are all refused. Statement types that can only be
# dangerous in LEADING position (``COPY``, ``SET``, ``DO``, ``COMMENT ON``,
# ...) are already blocked by the SELECT/WITH leading-keyword check and the
# single-statement rule, and are deliberately NOT in this list -- they are
# common column names and would false-positive legitimate read queries.
_FORBIDDEN_SQL = re.compile(
    r"\b("
    r"insert|update|delete|drop|alter|create|replace|truncate|merge|"
    r"grant|revoke|attach|detach|pragma|vacuum|"
    r"exec|execute|call|"
    r"into|returning|"
    # Known side-effecting / abuse-prone functions reachable from a bare
    # SELECT (defense in depth -- the read-only DB ROLE is the real gate):
    # sequence movement, large-object I/O, remote execution, extension
    # loading, and sleep/DoS primitives.
    r"nextval|setval|lastval|lo_import|lo_export|dblink\w*|load_extension|"
    r"pg_sleep|sleep|benchmark"
    r")\b",
    re.IGNORECASE,
)

_LEADING_SQL = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)

#: Audited table names must be plain identifiers (letters / digits / ``_`` /
#: space -- Frappe's ``tabLoan Application`` has a space). Anything else
#: (quotes, backticks, semicolons, comment tokens) is refused rather than
#: escaped: the audit runs with read credentials, but identifier smuggling
#: must still be impossible.
_TABLE_NAME = re.compile(r"^[A-Za-z0-9_ ]+$")


def assert_read_only_sql(query: str) -> str:
    """Validate that ``query`` is a single read-only SELECT; return it.

    Raises:
        ValueError: On an empty query, a non-SELECT leading keyword, statement
            stacking (any ``;`` beyond one optional trailing terminator), SQL
            comments (``--`` / ``/*`` / ``#`` -- refused outright so a
            forbidden keyword cannot hide behind one), or any mutating /
            DDL / control keyword anywhere in the statement.
    """
    text = (query or "").strip()
    if not text:
        raise ValueError("sql query is empty")
    if "--" in text or "/*" in text or "#" in text:
        raise ValueError(
            "sql query contains a comment token (--, /* or #) -- comments are "
            "refused in effect-verifier queries (comment smuggling)"
        )
    body = text[:-1] if text.endswith(";") else text
    if ";" in body:
        raise ValueError(
            "sql query stacks multiple statements (embedded ';') -- an effect "
            "verifier runs exactly one read-only SELECT"
        )
    if not _LEADING_SQL.match(body):
        raise ValueError(
            "sql query must start with SELECT (or WITH ... SELECT) -- the "
            "effect verifier is read-only by contract"
        )
    hit = _FORBIDDEN_SQL.search(body)
    if hit:
        raise ValueError(
            f"sql query contains forbidden keyword {hit.group(0)!r} -- the "
            "effect verifier must be read-only (no writes, DDL, or control "
            "statements; SELECT ... INTO is also refused)"
        )
    return text


class SqlRecordVerifier:
    """Verify effects against a SQL system of record with a read-only SELECT.

    Args:
        connect: Zero-arg callable returning a DB-API 2.0 connection (e.g.
            ``lambda: sqlite3.connect(path)`` or a configured
            ``pymysql.connect`` partial). A FRESH connection is opened and
            closed per read.
        query: A single read-only SELECT returning the candidate records
            (one row per record; column names become record keys). Validated
            by :func:`assert_read_only_sql` at construction -- a lexically
            mutating query refuses to construct (defense in depth; the
            connection's READ-ONLY database role is the real enforcement --
            see the module docstring). Use your driver's native parameter
            placeholders (``:name`` for sqlite3, ``%(name)s`` for
            pymysql/psycopg) for every dynamic value.
        query_params: Values bound through DB-API parameters (never
            interpolated). Bind run parameters here (the deployment layer
            resolves ``{param: ...}`` references before construction).
        timeout_s: Per-read budget guard (connect+execute happen inline; this
            bounds the polling loop, mirroring the REST verifier).
        poll_interval_s: Gap between polls while waiting for the write to
            land within ``Effect.timeout_s``.
    """

    substrate = "sql"

    def __init__(
        self,
        connect: Callable[[], Any],
        query: str,
        *,
        query_params: Optional[Mapping[str, Any]] = None,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.2,
    ) -> None:
        self._connect = connect
        self.query = assert_read_only_sql(query)
        self.query_params = dict(query_params or {})
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s

    # -- transport ----------------------------------------------------------

    def _fetch_records(self) -> Optional[list[dict[str, Any]]]:
        """Run the read-only query; rows as dicts keyed by column name.

        Returns ``None`` -- read as unreadable, forcing INDETERMINATE -- on
        any connect/execute/shape failure. Never raises.
        """
        try:
            conn = self._connect()
        except Exception:  # noqa: BLE001 - unreachable SoR is unreadable
            return None
        try:
            cursor = conn.cursor()
            if self.query_params:
                cursor.execute(self.query, self.query_params)
            else:
                cursor.execute(self.query)
            description = cursor.description
            if description is None:
                return None  # not a result-set query -> unusable
            columns = [str(col[0]) for col in description]
            rows = cursor.fetchall()
            return [dict(zip(columns, row)) for row in rows]
        except Exception:  # noqa: BLE001 - any read failure is unreadable
            return None
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - close failure changes nothing
                pass

    # -- EffectVerifier protocol --------------------------------------------

    def capture_pre_state(self, context: Any = None) -> EffectState:
        records = self._fetch_records()
        return EffectState(
            substrate=self.substrate,
            reachable=records is not None,
            records=records or [],
            detail={"query": self.query},
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        deadline = time.monotonic() + max(0.0, expected.timeout_s)
        while True:
            current = self._fetch_records()
            last = judge_records(expected, before, current, substrate=self.substrate)
            if last.confirmed or time.monotonic() >= deadline:
                return last
            time.sleep(self.poll_interval_s)


# -- exact table-delta audit (promoted from benchmark/frappe_lending) --------


def capture_table_counts(
    connect: Callable[[], Any], tables: Iterable[str]
) -> Optional[dict[str, int]]:
    """Exact ``COUNT(*)`` per audited table (read-only), or ``None``.

    ``None`` -- unreadable -- when the connection or any count fails; the
    caller must treat an unreadable audit as INDETERMINATE, never as "no
    change". Table names are validated against a strict identifier pattern
    and quoted with standard double quotes; anything fancier is refused.

    Raises:
        ValueError: On a table name that is not a plain identifier (identifier
            smuggling is a config error, not a runtime condition).
    """
    names = list(tables)
    for name in names:
        if not _TABLE_NAME.match(name):
            raise ValueError(
                f"audited table name {name!r} is not a plain identifier "
                "(letters/digits/underscore/space only)"
            )
    try:
        conn = connect()
    except Exception:  # noqa: BLE001
        return None
    try:
        counts: dict[str, int] = {}
        cursor = conn.cursor()
        for name in names:
            cursor.execute(f'SELECT COUNT(*) FROM "{name}"')
            row = cursor.fetchone()
            if row is None:
                return None
            counts[name] = int(row[0])
        return counts
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def audit_table_deltas(
    before: Mapping[str, int],
    after: Mapping[str, int],
    expected: Mapping[str, int],
) -> tuple[list[str], dict[str, int]]:
    """Exact-contract table-delta audit; violations plus the full delta map.

    The contract the governed Frappe Lending matrix proved (lifted from
    ``benchmark/frappe_lending/fixture.py::audit_table_deltas``): every table
    in ``expected`` must change by EXACTLY its declared delta, and every other
    observed table must change by exactly 0 -- a "successful" write that also
    perturbed an unrelated table is a violation, not noise.

    Returns:
        ``(violations, all_deltas)``: human-readable violation strings (empty
        when the contract holds exactly) and the complete per-table delta map
        for the audit trail.
    """
    all_deltas = {
        table: after.get(table, 0) - before.get(table, 0)
        for table in sorted(set(before) | set(after))
    }
    violations: list[str] = []
    for table, required in expected.items():
        observed = all_deltas.get(table, 0)
        if observed != required:
            violations.append(f"{table}:{observed:+d} (expected {required:+d})")
    for table, observed in all_deltas.items():
        if table not in expected and observed != 0:
            violations.append(f"{table}:{observed:+d} (expected +0)")
    return violations, all_deltas
