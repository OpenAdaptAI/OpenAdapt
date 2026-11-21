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
mutating/DDL keywords, no known side-effecting functions; checked on the
query's tokens, so literals and quoted names are never read as SQL) AT
CONSTRUCTION TIME, and values are bound through DB-API parameters (never
string-interpolated) so a run param cannot inject SQL. But a keyword filter
CANNOT prove a SELECT is side-effect-free on a real engine -- a Postgres
``SELECT some_udf(...)`` or vendor function can mutate state while passing
every lexical check. The REAL enforcement is the database role: run this
verifier under a dedicated READ-ONLY account (no INSERT/UPDATE/DELETE, no
EXECUTE on writing functions, no sequence privileges), as
``docs/EFFECT_KIT.md`` instructs. The filter then guards against config
mistakes; the role guards against everything. In between,
:func:`read_only_connection` asks the driver itself for a read-only session
(``PRAGMA query_only``, a ``READ ONLY`` transaction), so a write is refused
even when the account could make one.

Driver-neutral: the caller supplies a zero-arg ``connect`` callable returning
a DB-API 2.0 connection (``sqlite3.connect`` in tests and CI; ``pymysql`` /
``mariadb`` / ``psycopg`` in a deployment). The kit imports no driver itself.

Fail-safe: any connect/execute error reads as *unreadable* -> INDETERMINATE ->
HALT, never a guessed success. A fresh connection is opened per read so a
poll never judges a stale transaction snapshot.
"""

from __future__ import annotations

import re
import sqlite3
import time
from contextvars import ContextVar
from typing import Any, Callable, Iterable, Literal, Mapping, Optional

from openadapt_flow.runtime.effects._common import drop_record_fields, judge_records
from openadapt_flow.runtime.effects._mysql_read import (
    MYSQL_RESULT_CHARSET_REFUSAL,
    MySQLResultCharsetRefused,
    is_mysql_read,
    mysql_utf8mb4_cursor,
)
from openadapt_flow.runtime.effects.adapter import VerifierAdapterBase
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
)
from openadapt_flow.verification import VerificationTier

_SQL_READ_DIALECT_REFUSAL = "SQL read dialect is unidentified; declare sql_dialect"
_POSTGRESQL_DRIVER_MODULES = frozenset({"psycopg", "psycopg2", "pg8000"})

# A named refusal belongs to one read, including subclass calls through
# _fetch_records(). Outside a public read, that method still returns None.
_sql_read_refusal: ContextVar[str | None] = ContextVar(
    "_sql_read_refusal", default=None
)

# Words that could make a SELECT-leading statement mutate, checked on the
# query's TOKENS (see :func:`_sql_tokens`), never on its raw text, so a word
# inside a string literal or a quoted identifier is not a keyword. A bare
# keyword anywhere in the statement is refused (defense in depth beyond the
# leading-keyword + single-statement checks): a data-modifying CTE
# (``WITH x AS (INSERT ... RETURNING ...)``), ``SELECT ... INTO``, and
# sqlite/vendor escapes. Statement types that can only be dangerous in
# LEADING position (``COPY``, ``SET``, ``DO``, ``COMMENT ON``, ...) are
# already blocked by the SELECT/WITH leading check and the single-statement
# rule, and are deliberately NOT here: they are common column names.
_FORBIDDEN_WORDS = frozenset(
    {
        "insert", "update", "delete", "drop", "alter", "create", "replace",
        "truncate", "merge", "grant", "revoke", "attach", "detach", "pragma",
        "vacuum", "exec", "execute", "call", "into", "returning",
    }
)  # fmt: skip
# Known side-effecting / abuse-prone functions reachable from a bare SELECT
# (defense in depth -- the read-only DB ROLE is the real gate): sequence
# movement, large-object I/O, remote execution, extension loading,
# sleep/DoS primitives, and functions that run a query passed as text
# (``query_to_xml('DELETE ...')``; sqlite's ``eval`` extension). Refused as
# a call even when the name is quoted (``"nextval"('s')``) or schema
# qualified (``pg_catalog.pg_sleep(1)``).
_FORBIDDEN_FUNCTIONS = frozenset(
    {
        "nextval", "setval", "lastval", "lo_import", "lo_export",
        "load_extension", "pg_sleep", "sleep", "benchmark", "eval",
        "query_to_xml", "query_to_xmlschema", "query_to_xml_and_xmlschema",
        "pg_sleep_for", "pg_sleep_until", "get_lock", "load_file",
        "pg_read_file", "pg_read_binary_file", "pg_notify",
        "master_pos_wait", "source_pos_wait", "pg_terminate_backend",
        "pg_cancel_backend", "pg_ls_dir",
    }
)  # fmt: skip
# Locks (``pg_advisory_lock``, ``pg_try_advisory_xact_lock_shared``, ...).
_FORBIDDEN_PREFIXES = ("dblink", "pg_advisory", "pg_try_advisory")

#: A ``%`` bind placeholder (``%s``, ``%(name)s``, psycopg's ``%b``/``%t``).
#: When parameters are passed, pymysql and psycopg2 substitute these on the
#: client without regard to quotes, so one inside a literal splices the
#: escaped value in as bare SQL: a parameter value could change the query.
#: ``%%`` (the escaped percent sign) is not one. Without parameters neither
#: driver formats the query, and no driver binds ``:name`` inside a literal.
_PLACEHOLDER = re.compile(r"%[sbt(]")

#: A Postgres dollar-quote opener (``$$`` or ``$tag$``): a string form this
#: lexer does not read, so it is refused rather than guessed.
_DOLLAR_QUOTE = re.compile(r"\$[A-Za-z_]*\$")


def _forbidden_function(name: str) -> bool:
    return name in _FORBIDDEN_FUNCTIONS or name.startswith(_FORBIDDEN_PREFIXES)


def _is_word_char(char: str) -> bool:
    return char == "_" or char.isalnum() or ord(char) > 127


def _sql_token_spans(
    text: str, *, preserve_quoted_case: bool = False
) -> list[tuple[str, str, int, int]]:
    """Split ``text`` into ``(kind, value, start, end)`` tokens, or raise ``ValueError``.

    Kinds: ``word`` (a bare word, lower-cased), ``quoted`` (a ``"..."`` or
    ```...``` identifier's text, lower-cased), ``string`` (a ``'...'``
    literal), ``punct`` (one other character). Whitespace is dropped.
    ``preserve_quoted_case`` retains exact quoted identifiers for native
    relation lookup; the read-only guard keeps its existing folding.

    It refuses, rather than guesses, every form whose reading differs
    between dialects or that can hide text from the checks: a comment
    (``--``, ``/*``, ``#``) outside a literal; a backslash anywhere (MySQL
    reads ``\'`` inside a literal as an escaped quote, standard SQL as
    the literal's end); a dollar-quoted string; an unterminated literal or
    identifier; a NUL character.
    """

    if "\0" in text:
        raise ValueError("sql query contains a NUL character")
    if "\\" in text:
        raise ValueError(
            "sql query contains a backslash -- its meaning inside a literal "
            "differs by dialect (MySQL escapes, standard SQL does not), so it "
            "is refused"
        )
    tokens: list[tuple[str, str, int, int]] = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char.isspace():
            i += 1
            continue
        if char in "'\"`":
            j = i + 1
            body: list[str] = []
            while True:
                if j >= n:
                    raise ValueError(
                        "sql query has an unterminated quoted literal or identifier"
                    )
                if text[j] == char:
                    if j + 1 < n and text[j + 1] == char:  # a doubled quote
                        body.append(char)
                        j += 2
                        continue
                    break
                body.append(text[j])
                j += 1
            value = "".join(body)
            tokens.append(
                ("string", value, i, j + 1)
                if char == "'"
                else (
                    "quoted",
                    value if preserve_quoted_case else value.lower(),
                    i,
                    j + 1,
                )
            )
            i = j + 1
            continue
        if text.startswith("--", i) or text.startswith("/*", i) or char == "#":
            raise ValueError(
                "sql query contains a comment token (--, /* or #) -- comments are "
                "refused in effect-verifier queries (comment smuggling)"
            )
        if char == "$" and _DOLLAR_QUOTE.match(text, i):
            raise ValueError("sql query contains a dollar-quoted string ($$ or $tag$)")
        if char == "$" and i + 1 < n and (text[i + 1].isalpha() or text[i + 1] == "_"):
            # sqlite reads ``$name(...)`` as one variable token, quotes and
            # all, so a quote inside it would hide what follows from this
            # lexer. Only the ``$1`` placeholder form is read.
            raise ValueError("sql query contains a $name variable; use $1 or :name")
        if char == "[":
            # sqlite reads ``[...]`` up to the first ``]`` as an identifier,
            # quotes and all. Text there must lex the same way on its own
            # (Postgres ``j['k']`` does), or a quote inside would open a
            # literal here that hides what sqlite runs after the ``]``.
            close = text.find("]", i + 1)
            segment = text[i + 1 : close] if close >= 0 else ""
            if any(q in segment for q in "'\"`"):
                try:
                    _sql_token_spans(segment)
                except ValueError:
                    raise ValueError(
                        "sql query has a quote inside [...] that doesn't close "
                        "before the ']' (sqlite reads [...] as one identifier)"
                    ) from None
        if _is_word_char(char):
            j = i
            # A number never absorbs a ``$``: ``1$$...$$`` is 1 and a
            # dollar-quoted string to Postgres, so the ``$$`` is read (and
            # refused) on its own.
            number = char.isdigit()
            while j < n and (_is_word_char(text[j]) or (text[j] == "$" and not number)):
                j += 1
            tokens.append(("word", text[i:j].lower(), i, j))
            i = j
            continue
        tokens.append(("punct", char, i, i + 1))
        i += 1
    return tokens


def _sql_tokens(
    text: str, *, preserve_quoted_case: bool = False
) -> list[tuple[str, str]]:
    """Split ``text`` into ``(kind, value)`` tokens, or raise ``ValueError``."""

    return [
        (kind, value)
        for kind, value, _, _ in _sql_token_spans(
            text, preserve_quoted_case=preserve_quoted_case
        )
    ]


#: Audited table names must be plain identifiers (letters / digits / ``_`` /
#: space -- Frappe's ``tabLoan Application`` has a space). Anything else
#: (quotes, backticks, semicolons, comment tokens) is refused rather than
#: escaped: the audit runs with read credentials, but identifier smuggling
#: must still be impossible.
_TABLE_NAME = re.compile(r"^[A-Za-z0-9_ ]+$")


#: Drivers :func:`read_only_connection` can make read-only, and how.
READ_ONLY_DRIVERS = frozenset(
    {"sqlite3", "psycopg", "psycopg2", "pymysql", "MySQLdb", "mysql.connector"}
)


def _known_non_mysql_read(conn: Any, dialect: Optional[str]) -> bool:
    return (
        dialect in ("sqlite", "postgresql")
        or isinstance(conn, sqlite3.Connection)
        or any(
            cls.__module__.partition(".")[0] in _POSTGRESQL_DRIVER_MODULES
            for cls in type(conn).__mro__
        )
    )


def _note_sql_read_refusal(reason: str) -> None:
    # An empty string marks an active read without a named refusal.
    if _sql_read_refusal.get() == "":
        _sql_read_refusal.set(reason)


def read_only_connection(conn: Any, driver: str) -> Any:
    """``conn`` with the driver's own read-only session switched on.

    Every transaction the read-back opens is then read-only on the server,
    whatever the query says. Raises ``ValueError`` for a driver this can't
    do for, so a caller that needs it fails closed.
    """

    if driver == "sqlite3":
        conn.execute("PRAGMA query_only = ON")
    elif driver == "psycopg2":
        conn.set_session(readonly=True)
    elif driver == "psycopg":
        conn.read_only = True
    elif driver in ("pymysql", "MySQLdb", "mysql.connector"):
        cursor = conn.cursor()
        try:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
        finally:
            cursor.close()
    else:
        raise ValueError(f"no driver-level read-only session for {driver!r}")
    return conn


class _SourceSqliteConnection(sqlite3.Connection):
    """Observe only a reader-owned fresh connection, preserving other readers."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.read_sources: set[tuple[Optional[str], Optional[str], Optional[str]]] = (
            set()
        )

        def observe(
            action: int,
            table: Optional[str],
            column: Optional[str],
            database: Optional[str],
            view: Optional[str],
        ) -> int:
            if action == sqlite3.SQLITE_READ:
                self.read_sources.add((database, table, view))
            return sqlite3.SQLITE_OK

        # Installed before this fresh connection is returned. An unknown
        # connector's existing security authorizer is never replaced.
        self.set_authorizer(observe)


def sqlite_read_connection(database: str) -> sqlite3.Connection:
    """A fresh read-only SQLite connection with native read-source observation."""

    return read_only_connection(
        sqlite3.connect(database, factory=_SourceSqliteConnection), "sqlite3"
    )


def _direct_table_source(query: str) -> Optional[tuple[str, ...]]:
    """Closed query shape over the existing lexer, never inferred SQL origin.

    Only ``SELECT * FROM table [WHERE ...]`` is eligible. Native metadata
    must still prove that this name resolved to the witnessed physical table.
    Joins, aliases, CTEs, projections and subqueries remain unknown.
    """

    try:
        tokens = _sql_tokens(query, preserve_quoted_case=True)
    except ValueError:
        return None
    if tokens and tokens[-1] == ("punct", ";"):
        tokens.pop()
    if tokens[:3] != [("word", "select"), ("punct", "*"), ("word", "from")]:
        return None
    i, names = 3, []
    while i < len(tokens) and _name_token(tokens[i]):
        if tokens[i][0] == "word" and not tokens[i][1].isascii():
            return None  # native unquoted case folding is dialect-dependent
        names.append(tokens[i][1])
        i += 1
        if i >= len(tokens) or tokens[i] != ("punct", "."):
            break
        i += 1
        if i >= len(tokens) or not _name_token(tokens[i]):
            return None
    if not 1 <= len(names) <= 2:
        return None
    tail = tokens[i:]
    if tail and (tail[0] != ("word", "where") or len(tail) == 1):
        return None
    if any(
        (
            kind == "word"
            and value
            in {
                "select",
                "from",
                "join",
                "union",
                "intersect",
                "except",
                "with",
                "as",
                "group",
                "having",
                "window",
                "order",
                "limit",
                "offset",
                "fetch",
                "for",
            }
        )
        or (kind == "punct" and value == ";")
        for kind, value in tail
    ):
        return None
    return tuple(names)


def _prepare_source(conn: Any, query: str) -> Optional[tuple[str, str, int]]:
    names = _direct_table_source(query)
    if names is None:
        return None
    if isinstance(conn, _SourceSqliteConnection):
        if len(names) == 2 and names[0].casefold() != "main":
            return None
        return ("main", names[-1], 0)
    if type(conn).__module__.split(".")[0] != "psycopg2":
        return None
    # Only the deployment reader's fresh read-only connection is probed.
    # An existing transaction may contain a caller's security settings.
    if conn.get_transaction_status() != 0 or conn.readonly is not True:
        return None
    cur = conn.cursor()
    try:
        relation = ".".join('"' + name.replace('"', '""') + '"' for name in names)
        # Resolve before executing the read: a function in its WHERE clause
        # cannot change search_path and make a view look like its base table.
        # An inherited read can return child rows while native column OIDs
        # name the parent; only a relation without descendants proves origin.
        cur.execute(
            "SELECT n.nspname, c.relname, c.oid FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.oid = pg_catalog.to_regclass(%s) AND c.relkind = 'r' "
            "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_inherits i "
            "WHERE i.inhparent = c.oid)",
            (relation,),
        )
        row = cur.fetchone()
        return None if row is None else (str(row[0]), str(row[1]), int(row[2]))
    except Exception:  # noqa: BLE001 - unavailable origin never excuses a row
        conn.rollback()  # this was a fresh reader-owned connection
        return None
    finally:
        cur.close()


def _confirm_source(
    conn: Any, description: Any, prepared: Optional[tuple[str, str, int]]
) -> Optional[tuple[str, str, int]]:
    if prepared is None:
        return None
    if isinstance(conn, _SourceSqliteConnection):
        if len(conn.read_sources) != 1:
            return None
        database, table, view = next(iter(conn.read_sources))
        if (
            database == "main"
            and table
            and view is None
            and (table.casefold() == prepared[1].casefold())
        ):
            return (database, table, 0)
        return None
    if (
        type(conn).__module__.split(".")[0] == "psycopg2"
        and description
        and all(
            getattr(column, "table_oid", None) == prepared[2]
            and (getattr(column, "table_column", None) or 0) > 0
            for column in description
        )
    ):
        return prepared
    return None


def _name_token(token: tuple[str, str]) -> bool:
    """A quoted name, or a word that isn't a number (``1.`` is a number)."""

    kind, value = token
    return kind == "quoted" or (kind == "word" and not value[:1].isdigit())


def _qualified(tokens: list[tuple[str, str]], index: int) -> bool:
    """Whether ``tokens[index]`` is part of a qualified name: a ``.`` with a
    name on its other side (``schema.call``, ``call.id``). ``1. INTO`` is
    not: the token before the ``.`` is a number."""

    dot = ("punct", ".")
    if index >= 2 and tokens[index - 1] == dot and _name_token(tokens[index - 2]):
        return True
    return (
        index + 2 < len(tokens)
        and tokens[index + 1] == dot
        and _name_token(tokens[index + 2])
    )


def assert_read_only_sql(query: str, *, binds: bool = False) -> str:
    """Validate that ``query`` is a single read-only SELECT; return it.

    The query is split into tokens (:func:`_sql_tokens`) first, so string
    literals and quoted identifiers are read as such: a ``;`` inside
    ``'a;b'`` is not a second statement, a table named `` `call` `` is not
    ``CALL``, and ``REPLACE(phone, '-', '')`` is the string function, not
    the statement.

    ``binds`` says parameters will be passed with the query. Then a ``%``
    placeholder inside a literal or quoted name is refused (pymysql and
    psycopg2 would splice the value in as SQL); ``%%`` is fine.

    Raises:
        ValueError: On an empty query; a form the tokenizer refuses
            (comments, backslashes, dollar quotes, an unterminated literal);
            a first word other than SELECT or WITH; statement stacking (a
            ``;`` outside a literal anywhere but at the very end); a bare
            mutating / DDL / control keyword anywhere in the statement
            (``replace`` only as a function call, and a keyword next to a
            ``.`` is a qualified name, not a keyword); or a call to a known
            side-effecting function, quoted or qualified.
    """
    text = (query or "").strip()
    if not text:
        raise ValueError("sql query is empty")
    tokens = _sql_tokens(text)
    if binds and any(
        kind in ("string", "quoted") and _PLACEHOLDER.search(value.replace("%%", ""))
        for kind, value in tokens
    ):
        raise ValueError(
            "sql query has a bind placeholder inside a quoted literal or name -- "
            "bind the whole value instead (the driver splices it in as SQL); "
            "write a literal percent sign as %%"
        )
    if tokens and tokens[-1] == ("punct", ";"):
        tokens = tokens[:-1]
    if ("punct", ";") in tokens:
        raise ValueError(
            "sql query stacks multiple statements (a ';' outside a literal) -- "
            "an effect verifier runs exactly one read-only SELECT"
        )
    if not tokens or tokens[0] not in (("word", "select"), ("word", "with")):
        raise ValueError(
            "sql query must start with SELECT (or WITH ... SELECT) -- the "
            "effect verifier is read-only by contract"
        )
    for index, (kind, value) in enumerate(tokens):
        if kind not in ("word", "quoted"):
            continue
        after = tokens[index + 1] if index + 1 < len(tokens) else ("", "")
        if kind == "word" and (
            (value == "for" and after in (("word", "share"), ("word", "key")))
            or (
                value == "lock"
                and [t[1] for t in tokens[index + 1 : index + 4]]
                == ["in", "share", "mode"]
            )
        ):
            raise ValueError(
                "sql query takes a row lock (FOR SHARE, FOR KEY SHARE or LOCK IN "
                "SHARE MODE) -- the effect verifier only reads"
            )
        called = after == ("punct", "(")
        if called and _forbidden_function(value):
            raise ValueError(
                f"sql query calls forbidden function {value!r} -- the effect "
                "verifier must be read-only"
            )
        if kind == "quoted":
            continue  # a quoted name is an identifier, never a keyword
        if not (value in _FORBIDDEN_WORDS or _forbidden_function(value)):
            continue
        if called and value == "replace":
            continue  # REPLACE(text, from, to): the string function
        if not called and value != "into" and _qualified(tokens, index):
            continue  # a qualified name: schema.call, t.update
        raise ValueError(
            f"sql query contains forbidden keyword {value!r} -- the effect "
            "verifier must be read-only (no writes, DDL, or control "
            "statements; SELECT ... INTO is also refused). Quote a table or "
            'column with that name (`call` or "call")'
        )
    return text


_CLAUSE_KEYWORDS = frozenset(
    {
        "select",
        "from",
        "where",
        "on",
        "having",
        "group",
        "order",
        "limit",
        "offset",
        "union",
        "intersect",
        "except",
        "values",
        "with",
    }
)
_NULL_SAFE_CLAUSES = frozenset({"where", "on", "having"})
_PREDICATE_BEFORE = frozenset(
    {
        ("word", "where"),
        ("word", "and"),
        ("word", "or"),
        ("word", "on"),
        ("word", "having"),
        ("punct", "("),
    }
)
_PREDICATE_AFTER_WORDS = frozenset(
    {
        "and",
        "or",
        "order",
        "group",
        "having",
        "limit",
        "offset",
        "fetch",
        "union",
        "intersect",
        "except",
    }
)
_NON_COLUMN_WORDS = frozenset(
    {
        "null",
        "true",
        "false",
        "not",
        "and",
        "or",
        "where",
        "select",
        "from",
        "on",
        "having",
        "case",
        "when",
        "then",
        "else",
        "end",
        "in",
        "is",
        "like",
        "ilike",
        "between",
        "exists",
        "distinct",
    }
)


def _is_simple_ident(token: tuple[str, str, int, int]) -> bool:
    kind, value, _, _ = token
    if kind == "quoted":
        return True
    if kind == "word":
        return bool(value) and not value[0].isdigit() and value not in _NON_COLUMN_WORDS
    return False


def _col_ref_before(
    spans: list[tuple[str, str, int, int]], end_idx: int
) -> Optional[int]:
    """Return the start token index of a simple column ref ending at ``end_idx``, or None."""

    if end_idx < 1 or not _is_simple_ident(spans[end_idx - 1]):
        return None
    start = end_idx - 1
    dots = 0
    while (
        dots < 2
        and start >= 2
        and spans[start - 1][0:2] == ("punct", ".")
        and _is_simple_ident(spans[start - 2])
    ):
        start -= 2
        dots += 1
    if start > 0 and spans[start - 1][0:2] == ("punct", "."):
        return None
    return start


def _col_ref_after(
    spans: list[tuple[str, str, int, int]], start_idx: int
) -> Optional[int]:
    """Return the end token index (exclusive) of a simple column ref starting at ``start_idx``, or None."""

    n = len(spans)
    if start_idx >= n or not _is_simple_ident(spans[start_idx]):
        return None
    end = start_idx + 1
    dots = 0
    while (
        dots < 2
        and end + 1 < n
        and spans[end][0:2] == ("punct", ".")
        and _is_simple_ident(spans[end + 1])
    ):
        end += 2
        dots += 1
    if end < n and spans[end][0:2] == ("punct", "."):
        return None
    return end


def _is_predicate_before(spans: list[tuple[str, str, int, int]], idx: int) -> bool:
    """Return True if token ``idx - 1`` can start a standalone boolean predicate."""

    if idx <= 0:
        return False
    prev = spans[idx - 1][0:2]
    if prev == ("punct", "("):
        # Reject function calls like ``func(col = :p)``: an opening paren is a
        # grouping paren only when preceded by another predicate boundary.
        return _is_predicate_before(spans, idx - 1)
    return prev in _PREDICATE_BEFORE


def _is_predicate_after(spans: list[tuple[str, str, int, int]], idx: int) -> bool:
    """Return True if token ``idx`` cleanly terminates a boolean predicate."""

    n = len(spans)
    while idx < n and spans[idx][0:2] == ("punct", ")"):
        idx += 1
    if idx >= n:
        return True
    kind, value = spans[idx][0], spans[idx][1]
    if (kind, value) == ("punct", ";"):
        return True
    return kind == "word" and value in _PREDICATE_AFTER_WORDS


def _is_null_safe_op_before(
    spans: list[tuple[str, str, int, int]], ph_i: int
) -> Optional[int]:
    """If ``IS NOT DISTINCT FROM`` or ``<=>`` immediately precedes ``ph_i``, return its start index."""

    if ph_i >= 4 and [s[0:2] for s in spans[ph_i - 4 : ph_i]] == [
        ("word", "is"),
        ("word", "not"),
        ("word", "distinct"),
        ("word", "from"),
    ]:
        return ph_i - 4
    if (
        ph_i >= 3
        and spans[ph_i - 3][0:2] == ("punct", "<")
        and spans[ph_i - 2][0:2] == ("punct", "=")
        and spans[ph_i - 1][0:2] == ("punct", ">")
        and spans[ph_i - 3][3] == spans[ph_i - 2][2]
        and spans[ph_i - 2][3] == spans[ph_i - 1][2]
    ):
        return ph_i - 3
    return None


def _is_null_safe_op_after(
    spans: list[tuple[str, str, int, int]], ph_j: int
) -> Optional[int]:
    """If ``IS NOT DISTINCT FROM`` or ``<=>`` immediately follows ``ph_j``, return its end index."""

    n = len(spans)
    if ph_j + 4 <= n and [s[0:2] for s in spans[ph_j : ph_j + 4]] == [
        ("word", "is"),
        ("word", "not"),
        ("word", "distinct"),
        ("word", "from"),
    ]:
        return ph_j + 4
    if (
        ph_j + 3 <= n
        and spans[ph_j][0:2] == ("punct", "<")
        and spans[ph_j + 1][0:2] == ("punct", "=")
        and spans[ph_j + 2][0:2] == ("punct", ">")
        and spans[ph_j][3] == spans[ph_j + 1][2]
        and spans[ph_j + 1][3] == spans[ph_j + 2][2]
    ):
        return ph_j + 3
    return None


def _token_clauses(spans: list[tuple[str, str, int, int]]) -> list[str]:
    """Return the active SQL clause name at each token index."""

    stack: list[str] = [""]
    clauses: list[str] = []
    n = len(spans)
    for idx, (kind, value, _, _) in enumerate(spans):
        if kind == "punct" and value == "(":
            clauses.append(stack[-1])
            stack.append(stack[-1])
            continue
        if kind == "punct" and value == ")":
            if len(stack) > 1:
                stack.pop()
            clauses.append(stack[-1])
            continue
        if (
            kind == "word"
            and value in _CLAUSE_KEYWORDS
            and not (idx > 0 and spans[idx - 1][0:2] == ("punct", "."))
            and not (
                idx + 1 < n and spans[idx + 1][0:2] in (("punct", "."), ("punct", "("))
            )
            and not (
                value == "from"
                and idx > 0
                and spans[idx - 1][0:2] == ("word", "distinct")
            )
        ):
            stack[-1] = value
        clauses.append(stack[-1])
    return clauses


def _find_param_placeholders(
    text: str, spans: list[tuple[str, str, int, int]], param_name: str
) -> list[tuple[int, int]]:
    """Return ``(start_tok, end_tok)`` for every ``:name`` or ``%(name)s`` placeholder."""

    if not param_name:
        return []
    found: list[tuple[int, int]] = []
    n = len(spans)
    i = 0
    while i < n:
        kind, value, s0, e0 = spans[i]
        if (
            kind == "punct"
            and value == ":"
            and i + 1 < n
            and not (i > 0 and spans[i - 1][0:2] == ("punct", ":"))
            and spans[i + 1][0] == "word"
            and spans[i + 1][2] == e0
            and text[spans[i + 1][2] : spans[i + 1][3]] == param_name
        ):
            found.append((i, i + 2))
            i += 2
            continue
        if (
            kind == "punct"
            and value == "%"
            and i + 4 < n
            and not (i > 0 and spans[i - 1][0:2] == ("punct", "%"))
            and spans[i + 1][0:2] == ("punct", "(")
            and spans[i + 1][2] == e0
            and spans[i + 2][0] == "word"
            and spans[i + 2][2] == spans[i + 1][3]
            and text[spans[i + 2][2] : spans[i + 2][3]] == param_name
            and spans[i + 3][0:2] == ("punct", ")")
            and spans[i + 3][2] == spans[i + 2][3]
            and spans[i + 4][0] == "word"
            and spans[i + 4][2] == spans[i + 3][3]
            and text[spans[i + 4][2] : spans[i + 4][3]] == "s"
        ):
            found.append((i, i + 5))
            i += 5
            continue
        i += 1
    return found


def _inside_not_group(spans: list[tuple[str, str, int, int]], idx: int) -> bool:
    """Return True if token ``idx`` sits inside any ``NOT (...)`` grouping."""

    open_stack: list[int] = []
    for pos in range(idx):
        tok = spans[pos][0:2]
        if tok == ("punct", "("):
            open_stack.append(pos)
        elif tok == ("punct", ")") and open_stack:
            open_stack.pop()
    return any(
        open_pos > 0 and spans[open_pos - 1][0:2] == ("word", "not")
        for open_pos in open_stack
    )


def _analyze_null_param(
    text: str,
    spans: list[tuple[str, str, int, int]],
    clauses: list[str],
    param_name: str,
) -> Optional[tuple[list[tuple[int, int, str]], bool]]:
    """Analyze whether ``param_name`` can bind ``None`` safely in ``text``.

    Returns ``(rewrites, keep_param)`` where ``rewrites`` is a list of
    ``(char_start, char_end, replacement)`` edits and ``keep_param`` is True
    when at least one occurrence uses native null-safe equality
    (``IS NOT DISTINCT FROM`` or ``<=>``). Returns ``None`` if any occurrence
    is outside a provably null-safe exact column equality in ``WHERE``/``ON``/
    ``HAVING``, or if ``param_name`` does not appear in ``text``.
    """

    occurrences = _find_param_placeholders(text, spans, param_name)
    if not occurrences:
        return None
    rewrites: list[tuple[int, int, str]] = []
    keep_param = False
    for ph_i, ph_j in occurrences:
        if clauses[ph_i] not in _NULL_SAFE_CLAUSES or _inside_not_group(spans, ph_i):
            return None
        ph_s = spans[ph_i][2]
        ph_e = spans[ph_j - 1][3]

        # Form 1a: <col> IS NOT DISTINCT FROM <ph>  or  <col> <=> <ph>
        op_before = _is_null_safe_op_before(spans, ph_i)
        if op_before is not None and _is_predicate_after(spans, ph_j):
            lhs_start = _col_ref_before(spans, op_before)
            if lhs_start is not None and _is_predicate_before(spans, lhs_start):
                keep_param = True
                continue

        # Form 1b: <ph> IS NOT DISTINCT FROM <col>  or  <ph> <=> <col>
        op_after = _is_null_safe_op_after(spans, ph_j)
        if op_after is not None and _is_predicate_before(spans, ph_i):
            rhs_end = _col_ref_after(spans, op_after)
            if rhs_end is not None and _is_predicate_after(spans, rhs_end):
                keep_param = True
                continue

        # Form 2a: <col> = <ph>  ->  <col> IS NULL
        if (
            ph_i >= 2
            and spans[ph_i - 1][0:2] == ("punct", "=")
            and not (spans[ph_i - 2][0] == "punct" and spans[ph_i - 2][1] in "!<>=:")
            and not (
                ph_j < len(spans)
                and spans[ph_j][0] == "punct"
                and spans[ph_j][1] in "=>"
            )
            and _is_predicate_after(spans, ph_j)
        ):
            lhs_start = _col_ref_before(spans, ph_i - 1)
            if lhs_start is not None and _is_predicate_before(spans, lhs_start):
                eq_s = spans[ph_i - 1][2]
                prefix = " " if eq_s > 0 and not text[eq_s - 1].isspace() else ""
                suffix = " " if ph_e < len(text) and _is_word_char(text[ph_e]) else ""
                rewrites.append((eq_s, ph_e, f"{prefix}IS NULL{suffix}"))
                continue

        # Form 2b: <ph> = <col>  ->  <col> IS NULL
        if (
            _is_predicate_before(spans, ph_i)
            and ph_j < len(spans)
            and spans[ph_j][0:2] == ("punct", "=")
            and not (
                ph_j + 1 < len(spans)
                and spans[ph_j + 1][0] == "punct"
                and spans[ph_j + 1][1] in "=>"
            )
        ):
            rhs_end = _col_ref_after(spans, ph_j + 1)
            if rhs_end is not None and _is_predicate_after(spans, rhs_end):
                col_s = spans[ph_j + 1][2]
                col_e = spans[rhs_end - 1][3]
                prefix = " " if ph_s > 0 and _is_word_char(text[ph_s - 1]) else ""
                suffix = " " if col_e < len(text) and _is_word_char(text[col_e]) else ""
                rewrites.append(
                    (ph_s, col_e, f"{prefix}{text[col_s:col_e]} IS NULL{suffix}")
                )
                continue

        return None
    return rewrites, keep_param


def sql_param_is_null_safe(query: str, param_name: str) -> bool:
    """Return True iff every occurrence of ``param_name`` in ``query`` can bind ``None`` safely.

    A SQL read parameter may bind ``None`` only when ``query`` is a valid
    read-only SELECT and every occurrence of ``:param_name`` or
    ``%(param_name)s`` is an exact column equality (``col = :p``,
    ``:p = col``, ``IS NOT DISTINCT FROM``, or ``<=>``) inside ``WHERE``,
    ``ON``, or ``HAVING``. Any other position (``:p IS NULL``, ``LIKE``,
    ``IN``, ``LIMIT``, arithmetic, projections, or an unused parameter)
    returns ``False`` (fail closed).
    """

    try:
        text = assert_read_only_sql(query, binds=True)
        spans = _sql_token_spans(text)
    except ValueError:
        return False
    clauses = _token_clauses(spans)
    return _analyze_null_param(text, spans, clauses, param_name) is not None


def bind_null_sql_params(
    query: str, query_params: Optional[Mapping[str, Any]]
) -> tuple[str, dict[str, Any]]:
    """Rewrite null-valued SQL equality parameters to ``IS NULL`` (fail closed).

    Non-null parameters are untouched. For each key in ``query_params`` whose
    value is ``None``, every placeholder occurrence must be a provably
    null-safe exact column equality in ``WHERE``/``ON``/``HAVING``; otherwise
    raises :class:`ValueError`.
    """

    params = dict(query_params or {})
    text = assert_read_only_sql(query, binds=bool(params))
    null_keys = [k for k, v in params.items() if v is None]
    if not null_keys:
        return text, params

    spans = _sql_token_spans(text)
    clauses = _token_clauses(spans)
    all_rewrites: list[tuple[int, int, str]] = []
    kept_null_keys: set[str] = set()
    for key in null_keys:
        analyzed = _analyze_null_param(text, spans, clauses, key)
        if analyzed is None:
            raise ValueError(
                f"null SQL query parameter {key!r} can only bind to an exact "
                "column equality (=, IS NOT DISTINCT FROM, or <=>) in "
                "WHERE/ON/HAVING"
            )
        rewrites, keep_param = analyzed
        all_rewrites.extend(rewrites)
        if keep_param:
            kept_null_keys.add(key)

    rewritten = text
    for start, end, replacement in sorted(
        all_rewrites, key=lambda item: item[0], reverse=True
    ):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    remaining = {
        k: v for k, v in params.items() if v is not None or k in kept_null_keys
    }
    rewritten = assert_read_only_sql(rewritten, binds=bool(remaining))
    return rewritten, remaining


def read_cell(value: Any) -> Any:
    """A binary column as ``0x`` + hex, so records stay JSON-serializable.

    OpenEMR keys rows by ``uuid binary(16)``; a driver returns those as bytes,
    which a baseline (JSON) cannot hold. Equal bytes give equal text, so
    record identity and change detection are unaffected.
    """

    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()
    return value


class SqlRecordVerifier(VerifierAdapterBase):
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
        drop_fields: Record fields removed before judging (``["id"]`` judges
            records by their whole content); see :class:`~openadapt_flow.runtime.effects.RestRecordVerifier`.
        source_metadata: Observe native source metadata on reader-owned fresh
            connections. Unknown connectors keep their existing authorizers
            and expose no attribution witness.
        sql_driver: Trusted configured DB-API module name, if available.
            Native MySQL-family connections are also recognized directly.
        sql_dialect: Trusted dialect required for an opaque DB-API connector.
            Each connection must retain its session across statements.
    """

    substrate = "sql"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(
        self,
        connect: Callable[[], Any],
        query: str,
        *,
        query_params: Optional[Mapping[str, Any]] = None,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.2,
        drop_fields: tuple[str, ...] = (),
        source_metadata: bool = False,
        sql_driver: Optional[str] = None,
        sql_dialect: Optional[Literal["sqlite", "mysql", "postgresql"]] = None,
    ) -> None:
        self.drop_fields = tuple(drop_fields)
        self._connect = connect
        self.query, self.query_params = bind_null_sql_params(query, query_params)
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self._source_metadata = source_metadata
        self._sql_driver = sql_driver
        self._sql_dialect = sql_dialect
        #: Last captured read's native physical relation (schema, table, OID;
        #: OID is 0 for SQLite). In memory only; None means unknown.
        self.source_relation: Optional[tuple[str, str, int]] = None

    # -- transport ----------------------------------------------------------

    def _fetch_records(
        self, *, capture_source: bool = False
    ) -> Optional[list[dict[str, Any]]]:
        """Run the read-only query; rows as dicts keyed by column name.

        Returns ``None`` -- read as unreadable, forcing INDETERMINATE -- on
        any connect/execute/shape failure. Never raises.
        """
        self.source_relation = None
        try:
            conn = self._connect()
        except Exception:  # noqa: BLE001 - unreachable SoR is unreadable
            return None
        if conn is None:
            return None
        try:
            cursor = None
            if self._sql_dialect == "mysql" or is_mysql_read(conn, self._sql_driver):
                cursor = mysql_utf8mb4_cursor(conn)
            elif not _known_non_mysql_read(conn, self._sql_dialect):
                _note_sql_read_refusal(_SQL_READ_DIALECT_REFUSAL)
                return None
            prepared = (
                _prepare_source(conn, self.query)
                if capture_source and self._source_metadata
                else None
            )
            if isinstance(conn, _SourceSqliteConnection):
                conn.read_sources.clear()
            if cursor is None:
                cursor = conn.cursor()
            if self.query_params:
                cursor.execute(self.query, self.query_params)
            else:
                cursor.execute(self.query)
            description = cursor.description
            if description is None:
                return None  # not a result-set query -> unusable
            columns = [str(col[0]) for col in description]
            if not columns or len(set(columns)) != len(columns):
                return None  # A dict cannot preserve duplicate result columns.
            rows = cursor.fetchall()
            if any(len(row) != len(columns) for row in rows):
                return None  # zip must not silently drop cells or column names.
            self.source_relation = _confirm_source(conn, description, prepared)
            return drop_record_fields(
                [dict(zip(columns, map(read_cell, row))) for row in rows],
                self.drop_fields,
            )
        except MySQLResultCharsetRefused:
            _note_sql_read_refusal(MYSQL_RESULT_CHARSET_REFUSAL)
            return None
        except Exception:  # noqa: BLE001 - any read failure is unreadable
            return None
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - close failure changes nothing
                pass

    def _fetch_with_refusal(
        self, *, capture_source: bool = False
    ) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
        token = _sql_read_refusal.set("")
        try:
            records = (
                self._fetch_records(capture_source=True)
                if capture_source
                else self._fetch_records()
            )
            refusal = _sql_read_refusal.get()
            return (None, refusal) if refusal else (records, None)
        finally:
            _sql_read_refusal.reset(token)

    # -- EffectVerifier protocol --------------------------------------------

    def capture_pre_state(self, context: Any = None) -> EffectState:
        records, refusal = self._fetch_with_refusal(capture_source=True)
        detail = {"query": self.query}
        if refusal is not None:
            detail["read_error"] = refusal
        return EffectState(
            substrate=self.substrate,
            reachable=records is not None,
            records=records or [],
            # Complete declared SELECT result, not proof of whole-table scope.
            readback_complete=records is not None,
            detail=detail,
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        deadline = time.monotonic() + max(0.0, expected.timeout_s)
        while True:
            current, refusal = self._fetch_with_refusal()
            last = judge_records(expected, before, current, substrate=self.substrate)
            if refusal is not None:
                last.reason = refusal
                return last
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
