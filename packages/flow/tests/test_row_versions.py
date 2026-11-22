"""Row versions held from the baseline (``frame.row_versions``).

On PostgreSQL the verifier holds a read-only snapshot from the baseline and
counts the transactions that wrote each target row in the window. These
tests check that one save passes and that every lost update stops: another
user's save then the agent's stale save (with and without a gap), two
same-value rewrites, an edit and a revert against a found check, and a
target whose held read is gone. They also check that the probe writes the
version source beside an excuse only where it can derive one, and that a
learned bookkeeping excuse no longer hides a lost update.

The judge's arithmetic runs everywhere on recorded transaction ids; the
round trips need ``OPENADAPT_SNAPSHOT_POSTGRES``. Records are made up.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from openadapt_flow import learn_probe
from openadapt_flow.runtime.effects import row_versions as rv
from openadapt_flow.runtime.effects.frame import FrameSpec
from openadapt_flow.verifier import Contract
from tests import test_learned_excuses as _le
from tests.test_learned_excuses import (
    LOGIN,
    SEEN,
    Engine,
    check,
    learned_block,
)

_homes = _le._homes


@pytest.fixture(autouse=True)
def _no_leaked_holds() -> Any:
    yield
    assert not rv._HELD, "a held read was left open"


@pytest.fixture
def pg(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if not _le._PG:
        pytest.skip("set OPENADAPT_SNAPSHOT_POSTGRES")
    e = _le._postgres()
    try:
        for statement in e.ddl():
            e.run(statement)
        for table in ("app_user", "api_token", "note", "asset"):
            e.run(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"(SELECT MAX(id) FROM {table}))"
            )
        yield e
    finally:
        for fn in e.cleanup:
            fn()


SPEC = {"table": "asset", "key_column": "id"}
BOUND_READ = (
    'SELECT CAST(t."id" AS TEXT) AS "id", t."tag" AS "tag", t."name" AS "name", '
    't."assigned_to" AS "assigned_to", t."modified_at" AS "modified_at" '
    'FROM "asset" t'
)
_n = [0]


def request_bookkeeping(e: Engine, user: str = _le.ACTOR) -> None:
    """What the app changes on every request by ``user``: the moves a
    read-only probe learns (no added row)."""

    _n[0] += 1
    e.run("UPDATE app_user SET last_login = %s WHERE id = %s", f"r{_n[0]}", int(user))
    e.run(
        "UPDATE api_token SET last_seen = %s WHERE user_id = %s", f"r{_n[0]}", int(user)
    )


def rename_contract(e: Engine, *, versions: bool = True, found: bool = False) -> dict:
    """Rename asset LT-100 (an update), or check it is already named (a
    found check), with the bookkeeping on the agent's own rows excused."""

    doc = _le.asset_contract(e, learned_block(LOGIN, SEEN))
    if versions:
        # The held rows must be the ones the target surface reads the
        # record from (rv.binding_problem): one SELECT over "asset" t.
        doc["verifier"] = {
            "kind": "surfaces",
            "surfaces": {"record": _le._verifier(e, BOUND_READ)},
        }
        for effect in doc["effects"]:
            effect["probe"] = "surface=record"
        doc["frame"]["row_versions"] = {**SPEC, "target_surface": "record"}
    if found:
        doc["frame"]["target_fields"] = []
    return doc


def rename(e: Engine, name: str, user: str = _le.ACTOR) -> None:
    request_bookkeeping(e, user)
    e.run("UPDATE asset SET name = %s WHERE tag = 'LT-100'", name)


def _outcome(result: Any) -> str:
    return result.outcome.value


# -- the judge's arithmetic, on recorded transaction ids ---------------------------


class _Held:
    def __init__(self, rows: dict[str, tuple]) -> None:
        self._rows, self.closed = rows, False

    def rows(self, spec: Any, keys: Any) -> dict[str, tuple]:
        return {k: v for k, v in self._rows.items() if k in keys}

    def close(self) -> None:
        self.closed = True


class _Cur:
    def __init__(self, rows: list[tuple], members: Optional[list[tuple]]) -> None:
        self._rows, self._members, self._sql = rows, members, ""

    def execute(self, sql: str, *a: Any) -> None:
        self._sql = sql
        if "multixact" in sql and self._members is None:
            raise RuntimeError(
                "MultiXactId 105 has not been created yet -- apparent wraparound"
            )

    def fetchall(self) -> list[tuple]:
        return list(self._members or []) if "multixact" in self._sql else self._rows


class _Conn:
    def __init__(
        self, rows: list[tuple], members: Optional[list[tuple]] = None
    ) -> None:
        self._rows, self._members, self.autocommit = rows, members, False

    def cursor(self) -> _Cur:
        return _Cur(self._rows, self._members)

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def _judge(
    old: tuple,
    now: tuple,
    *,
    changed: bool,
    held: bool = True,
    members: Optional[list[tuple]] = None,
) -> rv.VersionVerdict:
    spec = rv.FrameRowVersions(**SPEC)
    h: Optional[_Held] = _Held({"1": old}) if held else None
    result = rv.judge_row_versions(
        spec, h, lambda: _Conn([("1", *now)], members), [("1", True, changed)]
    )
    if h is not None:
        assert h.closed
    return result


def test_judge_counts_writers() -> None:
    # Nobody wrote it: xmin unchanged.
    assert _judge(("100", "0"), ("100", "0"), changed=False).status == "held"
    # One transaction replaced the version the baseline saw.
    assert _judge(("100", "105"), ("105", "0"), changed=True).status == "held"
    # Two: the old version was replaced by 105, the current one is 107's.
    two = _judge(("100", "105"), ("107", "0"), changed=True)
    assert (two.status, two.problem) == ("refuted", "extra_steps")
    # An edit and a revert leave the content unchanged, still two writers.
    assert _judge(("100", "105"), ("107", "0"), changed=False).status == "refuted"
    # Changed, yet the row version didn't move: can't say who saved it.
    odd = _judge(("100", "0"), ("100", "0"), changed=True)
    assert (odd.status, odd.problem) == ("not_checked", "no_new_version")
    # A lock left on the old version (xmax) but no save: still nobody.
    assert _judge(("100", "103"), ("100", "103"), changed=False).status == "held"


def test_judge_never_reads_a_shared_lock_as_one_writer() -> None:
    """The old version's xmax may be a multixact id equal to the current
    xmin by chance; if that multixact names another updater, not checked."""

    v = _judge(
        ("100", "105"),
        ("105", "0"),
        changed=True,
        members=[("103", "upd"), ("104", "keysh")],
    )
    assert (v.status, v.problem) == ("not_checked", "ambiguous_xmax")
    # Lockers only, or the writer itself as the updater: still one writer.
    assert (
        _judge(
            ("100", "105"), ("105", "0"), changed=True, members=[("104", "keysh")]
        ).status
        == "held"
    )
    assert (
        _judge(
            ("100", "105"), ("105", "0"), changed=True, members=[("105", "nokeyupd")]
        ).status
        == "held"
    )


def test_judge_stops_without_the_held_read() -> None:
    v = _judge(("100", "0"), ("105", "0"), changed=True, held=False)
    assert (v.status, v.problem) == ("not_checked", "unreadable")


def test_judge_stops_on_a_missing_row() -> None:
    spec = rv.FrameRowVersions(**SPEC)
    v = rv.judge_row_versions(
        spec, _Held({}), lambda: _Conn([("1", "5", "0")]), [("1", True, True)]
    )
    assert (v.status, v.problem) == ("not_checked", "missing")
    # A record created in the window (a duplicate made instead of the
    # update) has no earlier version: never held.
    v = rv.judge_row_versions(spec, _Held({}), lambda: _Conn([]), [("9", False, False)])
    assert (v.status, v.problem) == ("not_checked", "created_target")


def test_spec_is_postgresql_only_and_plain_names() -> None:
    db = {"dialect": "postgresql", "read": {"sql_driver": "psycopg2"}}
    FrameSpec.model_validate({"database": db, "row_versions": SPEC})
    with pytest.raises(ValueError, match="PostgreSQL only"):
        FrameSpec.model_validate(
            {
                "database": {"dialect": "mysql", "read": {"sql_driver": "pymysql"}},
                "row_versions": SPEC,
            }
        )
    with pytest.raises(ValueError, match="PostgreSQL only"):
        FrameSpec.model_validate({"row_versions": SPEC})
    with pytest.raises(ValueError, match="plain"):
        FrameSpec.model_validate(
            {"database": db, "row_versions": {"table": 'asset"; DROP TABLE x; --'}}
        )
    # Unset, it is left out, so existing contracts keep their digests.
    assert "row_versions" not in FrameSpec.model_validate({"database": db}).model_dump()


# -- the probe writes the version source beside the excuse ------------------------


def _generated(dialect: str, query: str, *, read_same: bool = True) -> dict:
    read = {"sql_driver": "psycopg2", "sql_connect_args": {"host": "db"}}
    other = {"sql_driver": "psycopg2", "sql_connect_args": {"host": "elsewhere"}}
    return {
        "verifier": {
            "kind": "surfaces",
            "surfaces": {
                "record": {
                    "kind": "sql",
                    **(read if read_same else other),
                    "sql_query": query,
                }
            },
        },
        "effects": [
            {"kind": "record_written", "probe": "surface=record|x", "expected_count": 1}
        ],
        "frame": {"database": {"dialect": dialect, "read": read}},
    }


GENERATED = (
    'SELECT CAST(t."id" AS TEXT) AS "id", CAST(t."name" AS TEXT) AS "name", '
    'CASE WHEN CAST(t."minimum_stock" AS numeric) = CAST(%(v0)s AS numeric) '
    "THEN '1' ELSE '0' END AS \"p__minimum_stock\" FROM \"part_part\" t"
)


def test_detects_the_row_versions_of_a_generated_read() -> None:
    got = learn_probe.detect_row_versions(_generated("postgresql", GENERATED))
    assert got == {
        "source": "postgresql_xmin",
        "table": "part_part",
        "key_column": "id",
        "target_surface": "record",
    }


@pytest.mark.parametrize(
    "doc",
    [
        # MySQL and MariaDB: a marker can't count saves.
        _generated("mysql", GENERATED),
        # A read over another connection.
        _generated("postgresql", GENERATED, read_same=False),
        # A join or any shape the detector can't read: no source.
        _generated(
            "postgresql",
            'SELECT CAST(t."id" AS TEXT) AS "id" FROM "part_part" t JOIN x ON x.id = t.id',
        ),
        _generated("postgresql", 'SELECT CAST(t."pk" + 1 AS TEXT) AS "id" FROM "p" t'),
        # A read that also counts child rows: those rows have versions of
        # their own, which this source doesn't count.
        _generated(
            "postgresql",
            'SELECT CAST(t."id" AS TEXT) AS "id", CASE WHEN (SELECT COUNT(*) '
            "FROM \"part_tag\" g WHERE g.part_id = t.id) = 1 THEN '1' ELSE "
            '\'0\' END AS "p__tags" FROM "part_part" t',
        ),
    ],
)
def test_no_row_versions_where_none_can_be_derived(doc: dict) -> None:
    assert learn_probe.detect_row_versions(doc) is None


def test_probe_excuse_goes_in_only_with_a_version_source(tmp_path: Path) -> None:
    proposal = {
        "kinds": {"read_only_probe": {"excuses": [dict(LOGIN)]}},
        "proposal_sha256": "sha256:" + "0" * 64,
    }
    pg_doc = _generated("postgresql", GENERATED)
    my_doc = _generated("mysql", GENERATED)
    import yaml

    paths = []
    for name, doc in (("pg", pg_doc), ("my", my_doc)):
        doc["schema_version"] = "openadapt.verify/v1"
        doc["effects"][0]["match"] = {"name": {"param": "name"}}
        doc["verifier"]["surfaces"]["record"]["sql_password_env"] = "X"
        doc["frame"]["database"]["read"]["sql_password_env"] = "X"
        p = tmp_path / f"{name}.yaml"
        p.write_text(yaml.safe_dump(doc))
        paths.append(p)
    written = learn_probe.apply(proposal, paths)
    names = {p.stem for p, _n in written}
    assert names == {"pg"}
    pg_new = yaml.safe_load(paths[0].read_text())
    assert pg_new["frame"]["row_versions"]["table"] == "part_part"
    assert "learned" not in (yaml.safe_load(paths[1].read_text())["frame"] or {})
    Contract.from_dict(pg_new, name="pg")


# -- round trips on PostgreSQL ---------------------------------------------------


def test_one_save_passes(pg: Engine, tmp_path: Path) -> None:
    r = check(
        pg,
        rename_contract(pg),
        {"tag": "LT-100", "name": "Laptop B"},
        lambda: rename(pg, "Laptop B"),
        tmp_path,
    )
    assert _outcome(r) == "CONFIRMED", r.reasons


def test_bookkeeping_excuse_alone_hides_a_lost_update(
    pg: Engine, tmp_path: Path
) -> None:
    """Why #2413 holds these contracts back: excused bookkeeping, no
    version source, and a colleague's edit the stale save reverts."""

    def act() -> None:
        pg.run("UPDATE asset SET assigned_to = 3 WHERE tag = 'LT-100'")
        rename(pg, "Laptop B")
        pg.run("UPDATE asset SET assigned_to = NULL WHERE tag = 'LT-100'")

    r = check(
        pg,
        rename_contract(pg, versions=False),
        {"tag": "LT-100", "name": "Laptop B"},
        act,
        tmp_path,
    )
    assert _outcome(r) == "CONFIRMED", r.reasons


@pytest.mark.parametrize("gap", [False, True])
def test_lost_update_stops(pg: Engine, tmp_path: Path, gap: bool) -> None:
    """A colleague's save, then the agent's stale full save over it."""

    def act() -> None:
        pg.run("UPDATE asset SET assigned_to = 3 WHERE tag = 'LT-100'")
        if gap:
            import time

            time.sleep(1.0)
        request_bookkeeping(pg)
        pg.run(
            "UPDATE asset SET name = 'Laptop B', assigned_to = NULL WHERE tag = 'LT-100'"
        )

    r = check(
        pg,
        rename_contract(pg),
        {"tag": "LT-100", "name": "Laptop B"},
        act,
        tmp_path,
    )
    assert _outcome(r) == "REFUTED", r.reasons
    assert r.evidence["frame"]["versions"]["problem"] == "extra_steps"


def test_duplicate_instead_of_update_stops(pg: Engine, tmp_path: Path) -> None:
    """The agent adds a second record with the asked values instead of
    saving the existing one; the effect matches the new record."""

    def act() -> None:
        request_bookkeeping(pg)
        pg.run("INSERT INTO asset (tag, name) VALUES ('LT-100', 'Laptop B')")

    doc = rename_contract(pg)
    doc["frame"]["target_fields"] = ["name", "tag"]
    r = check(pg, doc, {"tag": "LT-100", "name": "Laptop B"}, act, tmp_path)
    assert _outcome(r) != "CONFIRMED", r.reasons


def test_same_value_rewrite_twice_stops(pg: Engine, tmp_path: Path) -> None:
    def act() -> None:
        rename(pg, "Laptop B")
        pg.run("UPDATE asset SET name = 'Laptop B' WHERE tag = 'LT-100'")

    r = check(
        pg,
        rename_contract(pg),
        {"tag": "LT-100", "name": "Laptop B"},
        act,
        tmp_path,
    )
    assert _outcome(r) == "REFUTED", r.reasons


def test_found_check(pg: Engine, tmp_path: Path) -> None:
    params = {"tag": "LT-100", "name": "Laptop A"}
    ok = check(
        pg,
        rename_contract(pg, found=True),
        params,
        lambda: request_bookkeeping(pg),
        tmp_path,
    )
    assert _outcome(ok) == "CONFIRMED", ok.reasons

    def edit_and_revert() -> None:
        # A colleague edits the record; the agent's stale save puts it back.
        pg.run("UPDATE asset SET assigned_to = 3 WHERE tag = 'LT-100'")
        request_bookkeeping(pg)
        pg.run("UPDATE asset SET assigned_to = NULL WHERE tag = 'LT-100'")

    bad = check(pg, rename_contract(pg, found=True), params, edit_and_revert, tmp_path)
    assert _outcome(bad) == "REFUTED", bad.reasons


def test_judge_without_the_held_read_stops(
    pg: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A judge in another process finds no held read: never a pass."""

    real_take = rv.take

    def gone(token: str) -> None:
        held = real_take(token)  # the other process never had it
        if held is not None:
            held.close()
        return None

    monkeypatch.setattr(rv, "take", gone)
    r = check(
        pg,
        rename_contract(pg),
        {"tag": "LT-100", "name": "Laptop B"},
        lambda: rename(pg, "Laptop B"),
        tmp_path,
    )
    assert _outcome(r) != "CONFIRMED"
    assert r.evidence["frame"]["versions"]["problem"] == "unreadable"


def test_held_read_is_a_read_only_snapshot(pg: Engine) -> None:
    from openadapt_flow.verify_run import _db_connect

    held = rv.open_held(rv.FrameRowVersions(**SPEC), _db_connect(pg.read))
    try:
        cur = held.conn.cursor()
        with pytest.raises(Exception, match="read-only"):
            cur.execute("UPDATE asset SET name = 'x'")
    finally:
        held.close()
    with pytest.raises(Exception):
        rv.open_held(rv.FrameRowVersions(table="no_such_table"), _db_connect(pg.read))


# -- review blockers: binding, multixact errors, one-table reads --------------


class _FailingCur(_Cur):
    def execute(self, sql: str, *a: Any) -> None:
        self._sql = sql
        if "multixact" in sql:
            raise RuntimeError(
                "permission denied for function pg_get_multixact_members"
            )


def test_any_other_multixact_error_is_not_checked() -> None:
    """Only "no such multixact" means a plain xid; any other failure (a
    permission error, a dropped connection) can't rule out two writers."""

    class Conn(_Conn):
        def cursor(self) -> _Cur:
            return _FailingCur(self._rows, self._members)

    spec = rv.FrameRowVersions(**SPEC)
    v = rv.judge_row_versions(
        spec,
        _Held({"1": ("100", "105")}),
        lambda: Conn([("1", "105", "0")]),
        [("1", True, True)],
    )
    assert v.status == "not_checked"


@pytest.mark.parametrize(
    "query",
    [
        'SELECT 1 AS "x" UNION SELECT CAST(t."id" AS TEXT) AS "id", t."n" AS "n" '
        'FROM "part_part" t',
        'WITH d AS (SELECT 1) SELECT CAST(t."id" AS TEXT) AS "id", t."n" AS "n" '
        'FROM "part_part" t',
        'SELECT CAST(t."id" AS TEXT) AS "id", t."n" AS "n" FROM "part_part" t; '
        'SELECT 1 FROM "part_part" t',
        'SELECT CAST(t."id" AS TEXT) AS "id", (SELECT 1) AS "n" FROM "part_part" t',
    ],
)
def test_a_read_that_is_not_one_select_over_one_table_has_no_source(
    query: str,
) -> None:
    assert rv.single_table_read(query, "id") is None
    assert learn_probe.detect_row_versions(_generated("postgresql", query)) is None


def _bound(spec: dict, query: str = GENERATED) -> Optional[str]:
    doc = _generated("postgresql", query)
    return rv.binding_problem(
        rv.FrameRowVersions(**spec),
        doc["verifier"]["surfaces"],
        doc["frame"]["database"]["read"],
        "id",
    )


def test_row_versions_must_be_the_target_surfaces_own_table() -> None:
    """A decoy table with the same key (one nobody edits) would vouch for a
    target it never held: an edit and a revert would pass."""

    good = {"table": "part_part", "key_column": "id", "target_surface": "record"}
    assert _bound(good) is None
    assert _bound({**good, "table": "decoy"}) is not None
    assert _bound({**good, "key_column": "other_id"}) is not None
    assert _bound({**good, "target_surface": "elsewhere"}) is not None
    assert _bound({"table": "part_part", "key_column": "id"}) is not None
    joined = GENERATED.replace('FROM "part_part" t', 'FROM "part_part" t JOIN x ON 1=1')
    assert _bound(good, query=joined) is not None


@pytest.mark.parametrize(
    "item",
    [
        'CAST(t."id" + 0 AS TEXT) AS "id"',
        'COALESCE(NULL, t."id") AS "id"',
        'lower(t."code") || t."id" AS "id"',
    ],
)
def test_a_computed_identity_is_not_the_key_column(item: str) -> None:
    """The returned identity must be the column itself (or its text cast),
    not an expression that ends with it."""

    query = f'SELECT {item}, t."n" AS "n" FROM "part_part" t'
    assert rv.single_table_read(query, "id") is None


def test_a_direct_or_cast_identity_is_the_key_column() -> None:
    for item in ('CAST(t."id" AS TEXT) AS "id"', 't."id" AS "id"'):
        query = f'SELECT t."n" AS "n", {item} FROM "part_part" t'
        assert rv.single_table_read(query, "id") == ("part_part", "id")


def test_a_key_naming_two_rows_is_not_checked() -> None:
    spec = rv.FrameRowVersions(**SPEC)
    two_now = rv.judge_row_versions(
        spec,
        _Held({"1": ("100", "105")}),
        lambda: _Conn([("1", "105", "0"), ("1", "107", "0")]),
        [("1", True, True)],
    )
    assert two_now.status == "not_checked"
    assert "more than one row" in two_now.reason

    class TwoHeld(_Held):
        def rows(self, spec: Any, keys: Any) -> dict[str, tuple]:
            return rv._unique_rows([("1", "100", "105"), ("1", "101", "0")])

    two_before = rv.judge_row_versions(
        spec, TwoHeld({}), lambda: _Conn([("1", "105", "0")]), [("1", True, True)]
    )
    assert two_before.status == "not_checked"


def test_an_expired_multixact_is_not_checked() -> None:
    """PostgreSQL says "does no longer exist" for a multixact older than its
    retained range too; its members are gone, so one writer can't be told."""

    class ExpiredCur(_Cur):
        def execute(self, sql: str, *a: Any) -> None:
            self._sql = sql
            if "multixact" in sql:
                raise RuntimeError("MultiXactId 105 does no longer exist -- wraparound")

    class Conn(_Conn):
        def cursor(self) -> _Cur:
            return ExpiredCur(self._rows, self._members)

    v = rv.judge_row_versions(
        rv.FrameRowVersions(**SPEC),
        _Held({"1": ("100", "105")}),
        lambda: Conn([("1", "105", "0")]),
        [("1", True, True)],
    )
    assert v.status == "not_checked"
