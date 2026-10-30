"""The read-only SQL guard reads tokens, not substrings.

Before, it refused ``REPLACE()``, a ``;`` inside a string literal, and any
query with the word ``call`` (EspoCRM keeps phone calls in a table named
`` `call` ``). Those now pass. Every write, multi-statement and smuggling
form below must still be refused; the read-only database role and session
remain the real gate (``docs/EFFECT_KIT.md``).
"""

from __future__ import annotations

import sqlite3

import pytest

from openadapt_flow.runtime.effects.sql import assert_read_only_sql

ALLOWED = [
    # EspoCRM: the call table, quoted as MySQL needs, or schema qualified.
    "SELECT c.id, c.name FROM `call` c WHERE c.deleted = 0",
    "SELECT c.id FROM espocrm.`call` c",
    'SELECT c.id FROM "call" c',
    "SELECT call.id FROM espocrm.call",
    "SELECT x.call_id FROM call_contact x",
    # A phone normalized the way the app stores it.
    "SELECT REPLACE(REPLACE(pn.name, '-', ''), ' ', '') AS phone FROM phone_number pn",
    "SELECT replace (name, 'a', 'b') FROM t",
    # A ';' or a comment marker inside a literal is text.
    "SELECT GROUP_CONCAT(name SEPARATOR ';') FROM t",
    "SELECT * FROM t WHERE note = 'a; DROP TABLE t'",
    "SELECT * FROM t WHERE note = '-- not a comment /* nor this */ # nor this'",
    "SELECT * FROM t WHERE note = 'it''s; fine'",
    # Keywords inside literals and quoted identifiers are not keywords.
    "SELECT 'insert', 'call me', \"update\" FROM \"delete\"",
    "SELECT `into`, `returning` FROM t",
    # A qualified column named like a keyword.
    "SELECT t.update, t.call FROM t",
    "SELECT * FROM t;",
    "SELECT * FROM t ;  ",
    "SELECT %(bot)s::int AS bot, :name AS n, $1 AS p FROM t",
]


@pytest.mark.parametrize("query", ALLOWED)
def test_reads_the_old_guard_refused_now_pass(query: str) -> None:
    assert assert_read_only_sql(query) == query.strip()


REFUSED = [
    # Writes and DDL, leading or anywhere.
    "DELETE FROM t",
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET a=1",
    "REPLACE INTO t VALUES (1)",
    "DROP TABLE t",
    "CALL purge()",
    "call purge()",
    "EXEC purge",
    "  (SELECT 1)",
    "SELECT * INTO backup FROM t",
    "SELECT * FROM t INTO OUTFILE '/tmp/x'",
    "WITH x AS (INSERT INTO t VALUES (1) RETURNING id) SELECT * FROM x",
    "WITH x AS (DELETE FROM t RETURNING id) SELECT * FROM x",
    "WITH x AS (UPDATE t SET a = 1 RETURNING id) SELECT * FROM x",
    "WITH x AS (SELECT 1) CALL purge()",
    "WITH x AS (SELECT 1) REPLACE INTO t SELECT * FROM x",
    "SELECT * FROM call",  # bare CALL: quote the table name
    "SELECT replace FROM t",
    "PRAGMA writable_schema=1",
    # Stacked queries.
    "SELECT * FROM t; DELETE FROM t",
    "SELECT 1;SELECT 2",
    "SELECT 1;;",
    "SELECT ';'; DROP TABLE t",
    "SELECT 'a'';'; DROP TABLE t",
    "select * from t where a = (select 1); attach database 'x' as y",
    'SELECT "a;b"; DROP TABLE t',
    "SELECT `a;b`; DROP TABLE t",
    # Comments outside literals.
    "SELECT * FROM t -- DELETE FROM t",
    "SELECT * FROM t --",
    "SELECT /* hidden */ * FROM t",
    "SELECT /*!50000 DELETE */ 1",
    "SELECT * FROM t # DELETE",
    "SELECT 'x' -- '",
    "SELECT 'x' /* ' */",
    # Dialect-specific escapes and quoting this lexer won't guess at.
    "SELECT 'a\\'; DROP TABLE t; -- '",  # MySQL: \' escapes the quote
    "SELECT E'\\x41'",
    'SELECT "a\\"; DROP TABLE t"',
    "SELECT $$; DROP TABLE t; $$",
    "SELECT $tag$x$tag$",
    "SELECT \\! rm",
    "SELECT 'unterminated",
    'SELECT "unterminated',
    "SELECT `unterminated",
    "SELECT 1\x00; DROP TABLE t",
    # Side-effecting functions, bare, quoted or schema qualified.
    "SELECT nextval('loan_seq')",
    "SELECT \"nextval\"('loan_seq')",
    "SELECT pg_catalog.nextval ('loan_seq')",
    "SELECT setval('loan_seq', 99)",
    "SELECT lo_import('/etc/passwd')",
    "SELECT dblink_exec('conn', 'x')",
    "SELECT \"dblink\"('conn', 'x')",
    "SELECT load_extension('/tmp/evil.so')",
    "WITH slow AS (SELECT pg_sleep(30)) SELECT * FROM slow",
    "SELECT pg_catalog.pg_sleep(1)",
    "SELECT sleep(10)",
    "SELECT benchmark(100000000, sha1('x'))",
    # A function that runs a query passed as text.
    "SELECT query_to_xml('DELETE FROM t', true, false, '')",
    "SELECT eval('DELETE FROM t')",
    "",
    "   ",
]


@pytest.mark.parametrize("query", REFUSED)
def test_writes_stacks_and_smuggling_are_refused(query: str) -> None:
    with pytest.raises(ValueError):
        assert_read_only_sql(query)


def test_a_semicolon_in_a_literal_reads_on_a_real_engine() -> None:
    con = sqlite3.connect(":memory:")
    con.executescript(
        'CREATE TABLE "call" (id INTEGER, phone TEXT, note TEXT);'
        "INSERT INTO \"call\" VALUES (1, '617-555-0100', 'a;b');"
    )
    query = assert_read_only_sql(
        "SELECT REPLACE(phone, '-', '') AS phone FROM \"call\" WHERE note = 'a;b'"
    )
    assert con.execute(query).fetchall() == [("6175550100",)]


# -- review round 1 ------------------------------------------------------------

#: ``1.`` is a number, not a qualifier, so the ``into`` after it is INTO
#: (MariaDB ran ``SELECT 1. INTO OUTFILE`` under a read-only session).
#: sqlite reads ``[x']`` as one identifier and ``$a(')`` as one variable,
#: so the quote inside must not open a literal that hides a call.
REVIEW_REFUSED = [
    "SELECT 1. INTO OUTFILE '/tmp/x'",
    "SELECT 1. INTO @v",
    "SELECT 1.into @v",
    "SELECT 1. INTO newtab",
    "SELECT t.into FROM t",
    "SELECT 1 AS [x'], load_extension(char(47,110,111)) AS [y'] FROM t",
    "SELECT [a\"], load_extension('x') AS [b\"] FROM t",
    "SELECT ['a]b'], load_extension('x') FROM t",
    "SELECT $a('), load_extension('x'), $b(') FROM t",
    "SELECT $name FROM t",
    "SELECT $_x FROM t",
]


@pytest.mark.parametrize("query", REVIEW_REFUSED)
def test_review_round_1_forms_are_refused(query: str) -> None:
    with pytest.raises(ValueError):
        assert_read_only_sql(query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT j['k'] FROM t",  # Postgres jsonb subscript
        "SELECT a[1], [plain] FROM t",
        'SELECT espocrm.call.id, "s".update FROM t',
        "SELECT x FROM t WHERE y = $1",
    ],
)
def test_review_round_1_reads_still_pass(query: str) -> None:
    assert assert_read_only_sql(query) == query


def test_the_hidden_call_is_what_sqlite_would_run() -> None:
    # The refused query really reaches load_extension on sqlite: the guard
    # closes a hole, it doesn't refuse something harmless.
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (a INTEGER)")
    query = "SELECT 1 AS [x'], load_extension('nope') AS [y'] FROM t"
    con.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(sqlite3.OperationalError, match="not authorized"):
        con.execute(query).fetchall()
    with pytest.raises(ValueError):
        assert_read_only_sql(query)


# -- review round 1, follow-up (#1665 comment 6529357038) ---------------------

FOLLOW_UP_REFUSED = [
    # A placeholder inside a literal or quoted name: pymysql and psycopg2
    # splice the value in as SQL (MariaDB returned every row for
    # p = " OR 1=1 OR ").
    "SELECT a FROM t WHERE name = '%(p)s'",
    "SELECT a FROM t WHERE name = '%s'",
    "SELECT a FROM t WHERE name = 'x%(p)sy'",
    "SELECT a FROM t WHERE name = '%b'",
    'SELECT "%(p)s" FROM t',
    "SELECT `%s` FROM t",
    "SELECT a FROM t WHERE name = ':name'",
    # A dollar quote right after a number: Postgres reads 1 then $$...$$.
    "SELECT 1$$'$$; DELETE FROM t; SELECT 1$$' x$$",
    "SELECT 1$$x$$",
    "SELECT 1$tag$x$tag$",
    # Locking reads.
    "SELECT * FROM t LOCK IN SHARE MODE",
    "SELECT * FROM t FOR SHARE",
    "SELECT * FROM t FOR KEY SHARE",
    "SELECT * FROM t for share nowait",
    "SELECT * FROM t FOR NO KEY UPDATE",
    # Functions with side effects or reads outside the database.
    "SELECT get_lock('x', 1)",
    "SELECT pg_advisory_lock(1)",
    "SELECT pg_advisory_xact_lock_shared(1)",
    "SELECT pg_try_advisory_lock(1)",
    "SELECT pg_sleep_for('1 second')",
    "SELECT pg_sleep_until(now())",
    "SELECT load_file('/etc/passwd')",
    "SELECT pg_read_file('postgresql.conf')",
    "SELECT pg_read_binary_file('postgresql.conf')",
    "SELECT pg_notify('channel', 'payload')",
    'SELECT pg_catalog."pg_advisory_lock"(1)',
]


@pytest.mark.parametrize("query", FOLLOW_UP_REFUSED)
def test_follow_up_forms_are_refused(query: str) -> None:
    with pytest.raises(ValueError):
        assert_read_only_sql(query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT a FROM t WHERE at = '14:30'",  # a time is not :name
        "SELECT a FROM t WHERE code LIKE 'abc%' AND pct = '100%'",
        "SELECT a FROM t WHERE name = %(p)s AND b = :b AND c = %s",
        "SELECT share, lock, t.key FROM t",  # column names, not locks
        "SELECT a$b, $1 FROM t",
        "SELECT price FROM t WHERE x = 'for share'",
    ],
)
def test_follow_up_reads_still_pass(query: str) -> None:
    assert assert_read_only_sql(query) == query
