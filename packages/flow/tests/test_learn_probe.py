"""Learning per-request bookkeeping from read-only requests (learn_probe).

A read-only probe reads the whole-database frame, sends GETs as the agent
and reads again; rows of the acting user that moved become ``frame.learned``
excuses with ``source: read_only_probe``. These tests check that a correct
write then passes, and attack what the probe must never excuse: another
user's row, another column of the actor's row, a column the contract names,
a row no user owns, extra added rows, and a probe that writes. Each runs on
SQLite, and on MariaDB/MySQL and PostgreSQL when a scratch server is given
(see ``test_learned_excuses``). Records are made up.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from openadapt_flow import learn, learn_probe
from openadapt_flow.generate.sql_store import Column, ForeignKey, Schema, Table
from openadapt_flow.verifier import Contract
from tests import test_learned_excuses as _le
from tests.test_learned_excuses import (
    ACTOR,
    Engine,
    _refuted,
    _title,
    asset_contract,
    check,
)
from tests.test_learned_excuses import note_contract as _note_contract

# The engines (SQLite, and MySQL or PostgreSQL when given) and the private
# ledger and seal homes, shared with test_learned_excuses.
engine = _le.engine
_homes = _le._homes

_n = [0]


def note_contract(e: Engine) -> dict[str, Any]:
    """Create a note: one new record (``count_new_only``), nothing else."""

    doc = _note_contract(e)
    doc.pop("record")
    doc["effects"] = [
        {
            "kind": "record_written",
            "match": {"title": {"param": "title"}, "body": {"param": "body"}},
            "expected_count": 1,
            "count_new_only": True,
            "identity_field": "id",
        }
    ]
    return doc


def _t(name: str, cols: list[str], pk: list[str], fks: list[ForeignKey] = ()) -> Table:
    columns = [Column(c, "varchar", True, None) for c in cols]
    return Table(name, columns, pk, list(fks))


def _schema(tables: list[Table]) -> Schema:
    return Schema("sqlite", "app", {t.name: t for t in tables})


def _stamp() -> str:
    _n[0] += 1
    return f"p{_n[0]}"


def app_reads(e: Engine, user: str = ACTOR, first: bool = True) -> None:
    """What the app writes when ``user`` sends any request: the user's
    last-login time and token use, and once a first-login property row."""

    s = _stamp()
    e.run("UPDATE app_user SET last_login = %s WHERE id = %s", s, int(user))
    e.run("UPDATE api_token SET last_seen = %s WHERE user_id = %s", s, int(user))
    if first:
        e.run(
            "INSERT INTO user_property (user_id, property, value) VALUES (%s, %s, %s)",
            int(user),
            f"first.{s}",
            s,
        )


def users(e: Engine) -> learn.Users:
    schema = _schema(
        [
            _t("app_user", ["id", "name", "email", "last_login"], ["id"]),
            _t(
                "api_token",
                ["id", "user_id", "issued_by", "label", "last_seen"],
                ["id"],
                [
                    ForeignKey("user_id", "app_user", "id"),
                    ForeignKey("issued_by", "app_user", "id"),
                ],
            ),
            _t(
                "user_property",
                ["user_id", "property", "value"],
                ["user_id", "property"],
                [ForeignKey("user_id", "app_user", "id")],
            ),
            _t("note", ["id", "title", "body", "stamp"], ["id"]),
        ]
    )
    return learn.users_from("app_user", "id", schema)


def run_probe(e: Engine, tmp_path: Path, send: Any, rounds: int = 3) -> dict:
    c = Contract.from_dict(note_contract(e), name="note")
    eps = learn_probe.probe(
        c,
        {"title": "Probe note", "body": "x"},
        kind="add_note",
        actor=ACTOR,
        send=send,
        rounds=rounds,
        workdir=tmp_path / "probe",
    )
    return learn_probe.propose(eps, users(e))


def excuses_of(proposal: dict) -> list[dict]:
    return proposal["kinds"]["read_only_probe"]["excuses"]


def write_contract(e: Engine, tmp_path: Path, proposal: dict) -> dict[str, Any]:
    path = tmp_path / "note.yaml"
    path.write_text(yaml.safe_dump(note_contract(e)))
    written = learn_probe.apply(proposal, [path])
    assert written and written[0][0] == path
    data = yaml.safe_load(path.read_text())
    assert data["frame"]["learned"]["source"] == "read_only_probe"
    return data


def first_once(e: Engine) -> Any:
    seen = [False]

    def send() -> set[str]:
        app_reads(e, first=not seen[0])
        seen[0] = True
        return {"q"}

    return send


def test_probe_learns_the_actors_bookkeeping_and_a_correct_write_passes(
    engine: Engine, tmp_path: Path
) -> None:
    t = _title()

    def write(first: bool = False) -> None:
        app_reads(engine, first=first)
        engine.run("INSERT INTO note (title, body) VALUES (%s, %s)", t, "Body text")

    before = check(
        engine,
        note_contract(engine),
        {"title": t, "body": "Body text"},
        write,
        tmp_path,
    )
    _refuted(before)

    proposal = run_probe(engine, tmp_path, first_once(engine))
    got = {(x["table"], x["change"], tuple(x["columns"])) for x in excuses_of(proposal)}
    assert got == {
        ("app_user", "changed", ("last_login",)),
        ("api_token", "changed", ("last_seen",)),
    }
    # The first-login row showed in one round only: not bookkeeping, so it is
    # neither excused nor held for approval; it stays a stop.
    assert proposal["kinds"]["read_only_probe"]["needs_approval"] == []
    contract = write_contract(engine, tmp_path, proposal)
    t = _title()
    after = check(engine, contract, {"title": t, "body": "Body text"}, write, tmp_path)
    assert after.outcome.value == "CONFIRMED", after.reasons
    assert after.evidence["frame"]["learned"]["source"] == "read_only_probe"
    # A row the app adds for the actor still stops until a person approves it.
    t = _title()
    _refuted(
        check(
            engine,
            contract,
            {"title": t, "body": "Body text"},
            lambda: write(first=True),
            tmp_path,
        )  # fmt: skip
    )


def _learned(engine: Engine, tmp_path: Path) -> dict[str, Any]:
    return write_contract(
        engine, tmp_path, run_probe(engine, tmp_path, first_once(engine))
    )


@pytest.mark.parametrize(
    "harm",
    [
        "UPDATE app_user SET last_login = 'x' WHERE id = 3",
        "UPDATE app_user SET email = 'changed@example.test' WHERE id = 2",
        "UPDATE app_user SET last_login = 'x', email = 'e@example.test' WHERE id = 2",
        "UPDATE api_token SET last_seen = 'x' WHERE user_id = 3",
        "INSERT INTO user_property (user_id, property, value) VALUES (3, 'a', 'b')",
        "INSERT INTO user_property (user_id, property, value) VALUES (2, 'extra', 'b')",
        "DELETE FROM api_token WHERE user_id = 2",
    ],
)
def test_probe_excuses_never_cover_a_harm(
    engine: Engine, tmp_path: Path, harm: str
) -> None:
    contract = _learned(engine, tmp_path)
    t = _title()

    def act() -> None:
        app_reads(engine, first=False)
        engine.run("INSERT INTO note (title, body) VALUES (%s, %s)", t, "Body text")
        engine.run(harm)

    _refuted(check(engine, contract, {"title": t, "body": "Body text"}, act, tmp_path))


def test_a_row_no_user_owns_is_never_learned(engine: Engine, tmp_path: Path) -> None:
    def send() -> set[str]:
        app_reads(engine, first=False)
        # A row that moves on every read but belongs to no user: listed as a
        # stop, never excused.
        engine.run("UPDATE asset SET modified_at = %s WHERE id = 1", _stamp())
        return set()

    proposal = run_probe(engine, tmp_path, send)
    body = proposal["kinds"]["read_only_probe"]
    assert {x["table"] for x in body["excuses"]} == {"app_user", "api_token"}
    assert any(s["table"] == "asset" for s in body["stays_a_stop"])


def test_another_users_row_moving_in_the_window_is_never_learned(
    engine: Engine, tmp_path: Path
) -> None:
    def send() -> set[str]:
        app_reads(engine, first=False)
        app_reads(engine, user="3", first=False)  # a colleague, at the same time
        return set()

    proposal = run_probe(engine, tmp_path, send)
    body = proposal["kinds"]["read_only_probe"]
    for x in body["excuses"]:
        assert x["relation"] == "actor_row"
    contract = write_contract(engine, tmp_path, proposal)
    t = _title()

    def act() -> None:
        app_reads(engine, first=False)
        app_reads(engine, user="3", first=False)
        engine.run("INSERT INTO note (title, body) VALUES (%s, %s)", t, "Body text")

    _refuted(check(engine, contract, {"title": t, "body": "Body text"}, act, tmp_path))


def test_a_column_the_probe_or_contract_names_is_never_learned(
    engine: Engine, tmp_path: Path
) -> None:
    def send() -> set[str]:
        app_reads(engine, first=False)
        return {"last_login"}  # the probe's query named it

    proposal = run_probe(engine, tmp_path, send)
    tables = {x["table"] for x in excuses_of(proposal)}
    assert "app_user" not in tables and "api_token" in tables


def test_nothing_moves_nothing_is_learned(engine: Engine, tmp_path: Path) -> None:
    proposal = run_probe(engine, tmp_path, lambda: set())
    assert excuses_of(proposal) == []
    path = tmp_path / "note.yaml"
    path.write_text(yaml.safe_dump(note_contract(engine)))
    assert learn_probe.apply(proposal, [path]) == []
    assert "learned" not in yaml.safe_load(path.read_text())["frame"]


def test_without_a_users_table_nothing_is_learned(
    engine: Engine, tmp_path: Path
) -> None:
    c = Contract.from_dict(note_contract(engine), name="note")
    eps = learn_probe.probe(c, {"title": "P", "body": "x"}, kind="k", actor=ACTOR,
                            send=first_once(engine), rounds=2,
                            workdir=tmp_path / "p")  # fmt: skip
    assert excuses_of(learn_probe.propose(eps, None)) == []


def test_a_probe_sends_read_only_requests_only() -> None:
    for bad in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(learn_probe.ProbeError):
            learn_probe.check_read_only([("GET", "/a"), (bad, "/b")])
    with pytest.raises(learn_probe.ProbeError):
        learn_probe.check_read_only([])
    learn_probe.check_read_only([("GET", "/a"), ("head", "/b")])


def test_a_contract_with_learned_excuses_is_not_overwritten(
    engine: Engine, tmp_path: Path
) -> None:
    proposal = run_probe(engine, tmp_path, first_once(engine))
    path = tmp_path / "note.yaml"
    path.write_text(yaml.safe_dump(note_contract(engine)))
    learn_probe.apply(proposal, [path])
    with pytest.raises(learn_probe.ProbeError):
        learn_probe.apply(proposal, [path])


def test_users_table_from_the_manifests_actor_parameter(tmp_path: Path) -> None:
    schema = _schema(
        [
            _t("app_user", ["id"], ["id"]),
            _t(
                "entry",
                ["id", "owner", "title"],
                ["id"],
                [ForeignKey("owner", "app_user", "id")],
            ),  # fmt: skip
        ]
    )
    (tmp_path / "c.yaml").write_text(
        yaml.safe_dump(
            {
                "effects": [
                    {
                        "kind": "record_written",
                        "match": {
                            "owner": {"param": "bot_user_id"},
                            "title": {"param": "title"},
                        },
                    }
                ]
            }  # fmt: skip
        )
    )

    def manifest(actor_params: list[str]) -> Path:
        m = tmp_path / "manifest.yaml"
        m.write_text(
            yaml.safe_dump(
                {
                    "actor_params": actor_params,
                    "kinds": {
                        "add_entry": {
                            "main_table": "entry",
                            "variants": [{"create": "c.yaml"}],
                        }
                    },
                }  # fmt: skip
            )
        )
        return m

    u = learn_probe.users_for_actor_params(manifest(["bot_user_id"]), schema)
    assert u is not None and (u.table, u.key) == ("app_user", "id")
    assert learn_probe.users_for_actor_params(manifest([]), schema) is None
    assert learn_probe.users_for_actor_params(manifest(["other"]), schema) is None


def test_an_update_without_a_version_source_gets_no_probe_excuse(
    engine: Engine, tmp_path: Path
) -> None:
    # Another user's edit that the agent's stale save reverts leaves the
    # target unchanged. Without versions, only the bookkeeping move stopped
    # such a write, so an update contract with no version source keeps it.
    proposal = run_probe(engine, tmp_path, first_once(engine))
    assert excuses_of(proposal)
    path = tmp_path / "asset.yaml"
    path.write_text(yaml.safe_dump(asset_contract(engine)))
    assert learn_probe.apply(proposal, [path]) == []
    assert "learned" not in yaml.safe_load(path.read_text())["frame"]

    def act() -> None:
        app_reads(engine, first=False)
        engine.run("UPDATE asset SET name = 'Laptop B' WHERE id = 1")

    _refuted(
        check(
            engine,
            yaml.safe_load(path.read_text()),
            {"tag": "LT-100", "name": "Laptop B"},
            act,
            tmp_path,
        )  # fmt: skip
    )


def test_a_move_in_one_round_only_is_not_excused(
    engine: Engine, tmp_path: Path
) -> None:
    """A person editing the acting user's own row during one probe round
    is not per-request bookkeeping: only moves every round shows are."""

    rounds = [0]

    def send() -> set[str]:
        app_reads(engine, first=False)
        if rounds[0] == 0:
            engine.run(
                "UPDATE app_user SET email = %s WHERE id = %s",
                f"{_stamp()}@example.test",
                int(ACTOR),
            )
        rounds[0] += 1
        return {"q"}

    got = {
        (x["table"], tuple(x["columns"]))
        for x in excuses_of(run_probe(engine, tmp_path, send))
    }
    assert not any(t == "app_user" and "email" in cols for t, cols in got)
    # The token row moved the same way every round: still bookkeeping.
    assert ("api_token", ("last_seen",)) in got


def test_one_round_learns_nothing(engine: Engine, tmp_path: Path) -> None:
    with pytest.raises(learn_probe.ProbeError):
        run_probe(engine, tmp_path, first_once(engine), rounds=1)


def test_a_create_plus_an_update_gets_no_probe_excuse(
    engine: Engine, tmp_path: Path
) -> None:
    """``record_written`` with ``count_new_only`` beside a ``field_equals``
    update is not create-only: the update can't see a lost update."""

    proposal = run_probe(engine, tmp_path, first_once(engine))
    doc = note_contract(engine)
    doc["effects"].append(
        {"kind": "field_equals", "match": {"id": {"literal": "1"}},
         "field": "title", "value": {"param": "title"}}
    )  # fmt: skip
    path = tmp_path / "mixed.yaml"
    path.write_text(yaml.safe_dump(doc))
    assert learn_probe.apply(proposal, [path]) == []
    assert "learned" not in yaml.safe_load(path.read_text())["frame"]


def test_versions_on_another_surface_do_not_cover_an_update() -> None:
    from types import SimpleNamespace as NS

    from openadapt_flow.runtime.effects.effect import EffectKind
    from openadapt_flow.runtime.effects.surfaces import surface_probe

    update = NS(kind=EffectKind.FIELD_EQUALS, probe=surface_probe("secondary"),
                expected_count=1, count_new_only=False)  # fmt: skip
    versions = NS(target_surface="primary")
    contract = NS(config=NS(frame=NS(versions=versions), effects=[update]))
    assert learn_probe._sees_its_own_harm(contract) is False
    contract.config.frame.versions = NS(target_surface=None)
    assert learn_probe._sees_its_own_harm(contract) is True
