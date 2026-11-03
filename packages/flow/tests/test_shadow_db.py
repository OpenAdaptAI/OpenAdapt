"""Shadow mode for a client that writes to its database directly.

The "client" here writes to SQLite itself, the way a thick client does; the
agent announces each action to the shadow. The real verifier (whole-database
frame) judges each action, and the shadow watches the database between
actions. Records are made up.
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.shadow import Route
from openadapt_flow.shadow_db import DatabaseShadow
from openadapt_flow.shadow_report import render_html, write_report
from openadapt_flow.verifier import Contract

AUDIT = "SELECT id FROM audit WHERE user = %(actor)s"


@pytest.fixture(autouse=True)
def _homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE patient (id INTEGER PRIMARY KEY, fname TEXT, lname TEXT, "
        "email TEXT, phone TEXT);"
        "CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, tbl TEXT);"
        "INSERT INTO patient VALUES (1, 'Wendell', 'Prior', 'w@example.test', '1');"
    )
    con.commit()
    con.close()
    return db


def _contract(db: Path) -> Contract:
    return Contract.from_dict(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "sql",
                "sqlite_database": str(db),
                "sql_query": "SELECT * FROM patient",
            },
            "record": {
                "name": "patient",
                "match": ["email", "lname"],
                "fields": {"fname": "fname", "lname": "lname", "email": "email"},
            },
            "frame": {
                "identity_field": "id",
                "database": {
                    "dialect": "sqlite",
                    "read": {"sqlite_database": str(db)},
                    "views": {"patient": ""},
                    "new_row_fields": {"patient": ["id", "phone"]},
                    "allow_tables": ["audit"],
                },
            },
        }
    )


class Client:
    """A thick client: it writes to its database itself, and logs who did."""

    def __init__(self, db: Path, user: str = "bot") -> None:
        self.db, self.user = db, user

    def sql(self, statement: str, args: tuple = (), user: str = "") -> None:
        con = sqlite3.connect(self.db)
        con.execute(statement, args)
        con.execute(
            "INSERT INTO audit (user, tbl) VALUES (?, 'patient')", (user or self.user,)
        )
        con.commit()
        con.close()

    def save(self, v: dict[str, str], *, fault: str = "") -> None:
        fname = v["first"] + ("x" if fault == "mutate" else "")
        for _ in range(2 if fault == "duplicate" else 1):
            self.sql(
                "INSERT INTO patient (fname, lname, email) VALUES (?, ?, ?)",
                (fname, v["last"], v["email"]),
            )
        if fault == "side_write":
            self.sql("UPDATE patient SET phone = '99' WHERE id = 1")


VALUES = {"first": "Ada", "last": "Quill", "email": "ada@example.test"}


def _shadow(tmp_path: Path, db: Path, **kw: Any) -> DatabaseShadow:
    route = Route(
        None,  # type: ignore[arg-type] - actions are named, not matched
        _contract(db),
        {"fname": "first", "lname": "last", "email": "email"},
        "create_patient",
    )
    kw.setdefault("audit_sql", AUDIT)
    return DatabaseShadow(
        routes=[route], actor_id="bot", out=tmp_path / "out", quiet_s=60, **kw
    ).start()


def _call(url: str, path: str, doc: Any) -> tuple[int, dict[str, Any]]:
    host, port = url.split("//")[1].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=60)
    conn.request(
        "POST", path, body=json.dumps(doc), headers={"Content-Type": "application/json"}
    )
    resp = conn.getresponse()
    out = resp.status, json.loads(resp.read() or b"{}")
    conn.close()
    return out


def _act(
    sh: DatabaseShadow,
    client: Client,
    values: dict[str, str] = VALUES,
    *,
    fault: str = "",
    claimed: str = "done",
) -> tuple[int, dict[str, Any]]:
    status, begun = _call(
        sh.url, "/begin", {"action": "create_patient", "values": values}
    )
    assert status == 200, begun
    client.save(values, fault=fault)
    return _call(sh.url, "/end", {"action_id": begun["action_id"], "claimed": claimed})


def _wait(sh: DatabaseShadow) -> None:
    deadline = time.time() + 30
    while time.time() < deadline:
        if all(e.checked for e in sh.episodes):
            return
        time.sleep(0.05)
    raise AssertionError("not checked in time")


def _verdicts(sh: DatabaseShadow) -> list[tuple[str, str]]:
    return [(e.kind, e.verdict) for e in sh.episodes]


def test_a_correct_action_is_confirmed_and_the_gaps_are_clean(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db)
    status, reply = _act(sh, Client(db))
    assert status == 202 and reply == {"action_id": 1}
    sh.stop()
    assert _verdicts(sh) == [("sql", "CONFIRMED")]
    doc = sh.results()
    assert doc["counts"]["CONFIRMED"] == 1 and doc["observer"] == "announced actions"
    assert doc["watch"] == {"gaps_watched": 2, "gaps_clean": 2, "audit": True}
    assert "sqlite database" in doc["upstream"]
    page = write_report(doc, tmp_path / "out")
    assert "announced" in page.read_text()


@pytest.mark.parametrize("fault", ["mutate", "duplicate", "side_write"])
def test_a_wrong_action_is_refuted(tmp_path: Path, fault: str) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db)
    _act(sh, Client(db), fault=fault)
    sh.stop()
    assert _verdicts(sh) == [("sql", "REFUTED")]


def test_an_unannounced_write_by_the_agents_user_is_refuted(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db)
    client = Client(db)
    _act(sh, client)
    _wait(sh)
    client.sql("UPDATE patient SET phone = '42' WHERE id = 1")  # no announcement
    _act(sh, client, {"first": "Bo", "last": "Reed", "email": "bo@example.test"})
    sh.stop()
    # The second action is begun before the change before it is judged.
    assert _verdicts(sh) == [
        ("sql", "CONFIRMED"),
        ("sql", "CONFIRMED"),
        ("gap", "REFUTED"),
    ]
    gap = sh.episodes[2]
    assert "the agent's user wrote between announced actions" in gap.reasons[0]
    assert gap.evidence["tables"]["patient"]["changed"] == 1
    assert sh.results()["refuted_by_kind"] == {
        "a write outside every announced action": 1
    }


def test_an_unannounced_write_after_the_last_action_is_caught_at_stop(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db)
    client = Client(db)
    _act(sh, client)
    _wait(sh)
    client.sql("DELETE FROM patient WHERE id = 1")
    sh.stop()
    assert _verdicts(sh) == [("sql", "CONFIRMED"), ("gap", "REFUTED")]


def test_a_write_before_the_first_action_is_caught(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db)
    client = Client(db)
    client.sql("UPDATE patient SET lname = 'Pryor' WHERE id = 1")
    _act(sh, client)
    sh.stop()
    assert _verdicts(sh) == [("sql", "CONFIRMED"), ("gap", "REFUTED")]


def test_another_users_change_between_actions_is_uncertain(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db)
    client = Client(db)
    _act(sh, client)
    _wait(sh)
    client.sql("UPDATE patient SET phone = '7' WHERE id = 1", user="front-desk")
    sh.stop()
    assert _verdicts(sh) == [("sql", "CONFIRMED"), ("gap", "UNCERTAIN")]
    assert "the audit names no write by the agent's user" in " ".join(
        sh.episodes[1].reasons
    )


def test_without_an_audit_a_change_between_actions_is_uncertain(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db, audit_sql=None)
    client = Client(db)
    _act(sh, client)
    _wait(sh)
    client.sql("UPDATE patient SET phone = '7' WHERE id = 1")
    sh.stop()
    assert _verdicts(sh) == [("sql", "CONFIRMED"), ("gap", "UNCERTAIN")]
    assert "who changed it wasn't checked" in " ".join(sh.episodes[1].reasons)


def test_an_unreadable_audit_never_reads_as_clean(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db, audit_sql="SELECT id FROM nope WHERE u = %(actor)s")
    client = Client(db)
    _act(sh, client)
    _wait(sh)
    client.sql("UPDATE patient SET phone = '7' WHERE id = 1")
    sh.stop()
    assert _verdicts(sh) == [("sql", "CONFIRMED"), ("gap", "UNCERTAIN")]
    assert "the audit couldn't be read" in " ".join(sh.episodes[1].reasons)


def test_an_unreadable_database_is_uncertain_and_the_agent_goes_on(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db)
    gone = str(tmp_path / "gone" / "x.sqlite")
    cfg = sh.routes[0].contract.config
    cfg.verifier.sqlite_database = gone
    cfg.frame.database.read["sqlite_database"] = gone
    status, reply = _act(sh, Client(db))
    assert status == 202
    sh.stop()
    kinds = _verdicts(sh)
    assert kinds[0] == ("sql", "UNCERTAIN")
    assert all(v == "UNCERTAIN" for _, v in kinds)


def test_a_failure_the_agent_reports_with_nothing_landed_is_uncertain(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db)
    status, begun = _call(
        sh.url, "/begin", {"action": "create_patient", "values": VALUES}
    )
    _call(sh.url, "/end", {"action_id": begun["action_id"], "claimed": "failed"})
    sh.stop()
    assert _verdicts(sh) == [("sql", "UNCERTAIN")]
    assert "the agent said 'failed'" in sh.episodes[0].reasons[0]


def test_claiming_done_when_nothing_landed_is_never_confirmed(tmp_path: Path) -> None:
    # The verifier can't rule out a write that lands later: UNCERTAIN
    # (reconciliation required), never CONFIRMED.
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db)
    status, begun = _call(
        sh.url, "/begin", {"action": "create_patient", "values": VALUES}
    )
    _call(sh.url, "/end", {"action_id": begun["action_id"]})
    sh.stop()
    assert _verdicts(sh) == [("sql", "UNCERTAIN")]
    assert "observed absent" in sh.episodes[0].reasons[0]


def test_overlapping_actions_are_both_uncertain(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db)
    client = Client(db)
    other = {"first": "Bo", "last": "Reed", "email": "bo@example.test"}
    _, a = _call(sh.url, "/begin", {"action": "create_patient", "values": VALUES})
    _, b = _call(sh.url, "/begin", {"action": "create_patient", "values": other})
    client.save(VALUES)
    client.save(other)
    _call(sh.url, "/end", {"action_id": a["action_id"]})
    _call(sh.url, "/end", {"action_id": b["action_id"]})
    sh.stop()
    assert [v for k, v in _verdicts(sh) if k == "sql"] == ["UNCERTAIN", "UNCERTAIN"]


def test_an_action_never_ended_is_uncertain(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db)
    _call(sh.url, "/begin", {"action": "create_patient", "values": VALUES})
    Client(db).save(VALUES)
    sh.stop()
    assert _verdicts(sh) == [("sql", "UNCERTAIN")]
    assert "stopped before the agent announced the end" in sh.episodes[0].reasons[0]


def test_an_unknown_action_or_missing_values_are_uncertain(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db)
    for doc in (
        {"action": "delete_everything", "values": VALUES},
        {"action": "create_patient", "values": {"first": "Ada"}},
    ):
        status, begun = _call(sh.url, "/begin", doc)
        assert status == 200 and begun["checked"] is False
        _call(sh.url, "/end", {"action_id": begun["action_id"]})
    sh.stop()
    assert [v for _, v in _verdicts(sh)] == ["UNCERTAIN", "UNCERTAIN"]


def test_end_rejects_an_unknown_or_repeated_action(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db)
    assert _call(sh.url, "/end", {"action_id": 9})[0] == 404
    assert _call(sh.url, "/end", {})[0] == 400
    _, begun = _act(sh, Client(db))
    assert _call(sh.url, "/end", {"action_id": begun["action_id"]})[0] == 409
    sh.stop()


def test_a_late_change_after_a_confirmed_action_is_refuted_on_the_recheck(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db)
    sh.routes[0].contract.config.frame.database.recheck_after_s = 1
    client = Client(db)
    _act(sh, client)
    _wait(sh)
    con = sqlite3.connect(db)  # a background job, with no audit entry
    con.execute("UPDATE patient SET fname = 'Adah' WHERE email = 'ada@example.test'")
    con.commit()
    con.close()
    time.sleep(1.2)
    sh.run_rechecks(now=True)
    sh.stop()
    first = sh.episodes[0]
    assert first.verdict == "CONFIRMED" and first.late_verdict == "REFUTED"


# -- the guard ------------------------------------------------------------------------


def test_the_guard_returns_the_verdict_and_halts_after_a_refutation(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db, guard=True)
    client = Client(db)
    status, reply = _act(sh, client)
    assert status == 200 and reply["verdict"] == "CONFIRMED" and not reply["halted"]
    other = {"first": "Bo", "last": "Reed", "email": "bo@example.test"}
    status, reply = _act(sh, client, other, fault="mutate")
    assert reply["verdict"] == "REFUTED" and "a person reviews it" in reply["halted"]
    status, refused = _call(
        sh.url, "/begin", {"action": "create_patient", "values": VALUES}
    )
    assert status == 409 and "is REFUTED" in refused["refused"]
    sh.stop()
    assert len(sh.results()["refusals"]) == 1


def test_the_guard_refuses_an_unknown_action_and_a_second_open_one(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db, guard=True)
    assert _call(sh.url, "/begin", {"action": "nope", "values": {}})[0] == 409
    status, _ = _call(sh.url, "/begin", {"action": "create_patient", "values": VALUES})
    assert status == 200
    status, refused = _call(
        sh.url, "/begin", {"action": "create_patient", "values": VALUES}
    )
    assert status == 409 and "still open" in refused["refused"]
    sh.stop()


def test_the_guard_refuses_when_the_database_cant_be_read(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db, guard=True)
    gone = str(tmp_path / "gone" / "x.sqlite")
    cfg = sh.routes[0].contract.config
    cfg.verifier.sqlite_database = gone
    cfg.frame.database.read["sqlite_database"] = gone
    status, refused = _call(
        sh.url, "/begin", {"action": "create_patient", "values": VALUES}
    )
    assert status == 409
    sh.stop()


def test_the_guard_refuses_after_an_unannounced_write(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db, guard=True)
    client = Client(db)
    _act(sh, client)
    client.sql("UPDATE patient SET phone = '42' WHERE id = 1")
    other = {"first": "Bo", "last": "Reed", "email": "bo@example.test"}
    status, refused = _call(
        sh.url, "/begin", {"action": "create_patient", "values": other}
    )
    assert status == 409 and "REFUTED between announced actions" in refused["refused"]
    sh.stop()


def test_the_report_shows_a_gap_finding(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db)
    client = Client(db)
    client.sql("UPDATE patient SET phone = '42' WHERE id = 1")
    sh.stop()
    page = render_html(sh.results())
    assert "between announced actions" in page and "REFUTED" in page


def test_every_contract_needs_the_database_frame(tmp_path: Path) -> None:
    from openadapt_flow.shadow import ShadowConfigError

    db = _db(tmp_path)
    contract = _contract(db)
    contract.config.frame.database = None
    with pytest.raises(ShadowConfigError, match="frame.database"):
        DatabaseShadow(
            routes=[Route(None, contract, {}, "a")],  # type: ignore[arg-type]
            out=tmp_path / "out",
        )
    with pytest.raises(ShadowConfigError, match="%\\(actor\\)s"):
        DatabaseShadow(
            routes=[Route(None, _contract(db), {}, "a")],  # type: ignore[arg-type]
            out=tmp_path / "out",
            audit_sql="SELECT id FROM audit",
        )


def test_the_cli_builds_database_mode_from_a_config(tmp_path: Path) -> None:
    import argparse

    import yaml

    from openadapt_flow import cli_shadow
    from openadapt_flow.shadow import ShadowConfigError

    db = _db(tmp_path)
    (tmp_path / "patient.verify.yaml").write_text(
        yaml.safe_dump(_contract(db).config.model_dump(mode="json", by_alias=True))
    )
    cfg = tmp_path / "shadow.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "mode": "db",
                "actor": "bot",
                "audit_sql": AUDIT,
                "actions": [
                    {
                        "name": "create_patient",
                        "contract": "patient.verify.yaml",
                        "params": {"fname": "first", "lname": "last", "email": "email"},
                    }
                ],
            }
        )
    )
    parser = argparse.ArgumentParser()
    cli_shadow.register(parser.add_subparsers())
    args = parser.parse_args(
        ["shadow", "--config", str(cfg), "--guard", "--listen", "0"]
    )
    sh = cli_shadow.build(cli_shadow.load(args), tmp_path / "out")
    assert isinstance(sh, DatabaseShadow) and sh.guard and sh.audit_sql == AUDIT
    sh.server.server_close()
    bad = parser.parse_args(
        ["shadow", "--mode", "db", "--upstream", "http://127.0.0.1:1"]
    )
    with pytest.raises(ShadowConfigError, match="no upstream"):
        cli_shadow.load(bad)


def test_a_gap_is_never_clean_when_triggers_cant_be_seen(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sh = _shadow(tmp_path, db)
    start = sh._mark()
    start.snapshot.code_visible = False
    gap = sh._judge_gap(start, sh._mark())
    sh.stop()
    assert gap is not None and gap.verdict == "UNCERTAIN"
    assert "triggers" in gap.reasons[0]
