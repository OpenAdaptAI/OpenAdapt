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
