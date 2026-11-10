"""Live test of #1505 on a real OpenEMR: the bot's unrelated write for the
patient it saved is never excused.

The bot saves a made-up patient through OpenEMR's Standard REST API (the
fixture's ``actor`` OAuth client). It then writes an allergy for that same
patient (a ``lists`` row) that the task never asked for. OpenEMR logs both
writes under the bot's user and the new patient's pid, so an audit-log frame
that excuses the bot's entries for the saved record by pid alone would excuse
the allergy too.

Two frames are judged on the same window:

- ``audit_read`` attribution without ``frame.database`` reads only
  ``patient_data``. It can't see the ``lists`` row, so it must not hold: the
  judge returns ``not_checked`` and ``verify-run`` can't report the save as
  verified.
- The same attribution with ``frame.database`` and OpenEMR's app profile diffs
  ``patient_data`` and ``lists`` row by row, and must refute: the allergy is
  the bot's write outside the declared delta.

What is real and what is not:

- The patient save goes through OpenEMR's REST API, and OpenEMR writes the
  rows and its own audit entries.
- The allergy is written with SQL plus the audit row OpenEMR would write for
  it (event ``patient-record-insert`` under the bot's user name, the statement
  base64-encoded in ``comments``, filed under the new pid). The fixture's
  OAuth client isn't granted the allergy scope, and the frame judges rows and
  log entries, not the channel that wrote them.
- As in ``tests/test_owner_attribution_live_openemr.py``, the database is read
  with ``SELECT`` statements through the fixture's ``docker compose exec``
  channel and the rows are passed to ``judge_frame``.

The module is skipped unless ``OPENEMR_FRAME_SCOPE_LIVE=1``. It needs the
local fixture already up and bootstrapped (``benchmark/openemr_local``; it
does not start containers), under the shared machine's lane-1 fixture lease.
Run it from ``packages/flow``::

    OPENEMR_FRAME_SCOPE_LIVE=1 PYTHONPATH=../types:. \\
        python -m pytest tests/test_frame_scope_live_openemr.py -v -p no:cacheprovider

``OPENEMR_FRAME_SCOPE_RESET=1`` restores the fixture's baseline database
afterwards (only for the session that holds the fixture). Without a reset,
the made-up patients and allergies stay.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from openadapt_flow.app_profiles import load_profile
from openadapt_flow.runtime.effects import Effect, EffectKind, EffectState, ValueExpr
from openadapt_flow.runtime.effects.frame import FrameSpec, FrameVerdict, judge_frame

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENEMR_FRAME_SCOPE_LIVE") != "1",
    reason=(
        "live OpenEMR: set OPENEMR_FRAME_SCOPE_LIVE=1 with the local fixture up "
        "(benchmark/openemr_local); see this module's docstring"
    ),
)

DB = "openemr"
#: The statement's table, from OpenEMR's logged SQL text.
_TABLE = re.compile(
    r"^\s*(?:insert\s+(?:ignore\s+)?into|replace\s+into|update|delete\s+from)\s+"
    r"`?(?:\w+`?\.`?)?(\w+)`?",
    re.IGNORECASE,
)
_TABLES = ("patient_data", "lists")


@pytest.fixture(scope="module")
def openemr() -> Iterator[Any]:
    from benchmark.openemr_local.qualify_hooks import fixture

    fx = fixture()
    yield fx
    if os.environ.get("OPENEMR_FRAME_SCOPE_RESET") == "1":
        fx.reset()


def _sql(fx: Any, sql: str) -> None:
    fx._db(sql)


def _lines(fx: Any, sql: str) -> list[str]:
    return list(fx._db_lines(sql))


def _rows(fx: Any, sql: str) -> list[list[str]]:
    return [line.split("\t") for line in _lines(fx, sql)]


def _quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _actor(fx: Any) -> str:
    return str(fx._runtime_values()["OPENEMR_ACTOR_USER"])


def _patients(fx: Any) -> list[dict[str, Any]]:
    return [
        {"id": r[0], "pid": r[1], "fname": r[2], "lname": r[3], "email": r[4]}
        for r in _rows(
            fx, f"SELECT id, pid, fname, lname, email FROM {DB}.patient_data"
        )
    ]


def _lists(fx: Any) -> list[dict[str, Any]]:
    return [
        {"id": r[0], "pid": r[1], "type": r[2], "title": r[3]}
        for r in _rows(fx, f"SELECT id, pid, type, title FROM {DB}.lists")
    ]


def _last_log_id(fx: Any) -> int:
    return int(_lines(fx, f"SELECT IFNULL(MAX(id), 0) FROM {DB}.log")[0])


def _statement(comments: bytes) -> str:
    """The logged statement, plain or as OpenEMR 8 stores it (base64)."""

    plain = comments.decode("utf-8", "replace")
    if _TABLE.match(plain):
        return plain
    cut = comments[: len(comments) - len(comments) % 4]
    try:
        return base64.b64decode(cut, validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return plain


def _window(fx: Any, after_id: int) -> list[dict[str, Any]]:
    """The window's write entries, each with the table its statement names.

    An entry whose statement is encrypted or names no table keeps an empty
    table, so it can explain nothing."""

    out = []
    for r in _rows(
        fx,
        "SELECT l.id, l.user, IFNULL(l.patient_id, ''), l.event, "
        "HEX(LEFT(l.comments, 300)), IFNULL(e.encrypt, 'No') "
        f"FROM {DB}.log l LEFT JOIN {DB}.log_comment_encrypt e ON e.log_id = l.id "
        f"WHERE l.id > {int(after_id)} ORDER BY l.id",
    ):
        log_id, user, patient_id, event, comments_hex, encrypted = r
        kind = event.rsplit("-", 1)[-1]
        if kind not in ("insert", "update", "delete"):
            continue
        match = (
            None
            if encrypted == "Yes"
            else _TABLE.match(_statement(bytes.fromhex(comments_hex)))
        )
        out.append(
            {
                "id": log_id,
                "user": user,
                "patient_id": patient_id or None,
                "kind": kind,
                "tbl": match.group(1).lower() if match else "",
            }
        )
    return out


def _attribution(actor: str) -> dict[str, Any]:
    return {
        "kind": "audit_read",
        "actor_field": "user",
        "self": [actor],
        "record_field": "patient_id",
        "record_key": "pid",
        "event_field": "kind",
        "write_events": ["insert", "update", "delete"],
        "insert_events": ["insert"],
        "delete_events": ["delete"],
        "table_field": "tbl",
        # Read by this module (``_window``), not by the judge.
        "read": {"kind": "sql", "sql_query": "SELECT 1"},
    }


def _spec(actor: str, *, whole_database: bool) -> FrameSpec:
    doc: dict[str, Any] = {"identity_field": "id", "attribution": _attribution(actor)}
    if whole_database:
        profile = load_profile("openemr")
        doc["database"] = {
            "dialect": "mysql",
            # The judge reads nothing; this module passes the rows.
            "read": {"sql_driver": "pymysql"},
            "profile": "openemr",
            "profile_sha256": profile.sha256(),
            "views": {"patient_data": "patient", "lists": "lists"},
        }
    return FrameSpec.model_validate(doc)


def _lname() -> str:
    letters = "".join(chr(ord("a") + int(c, 16) % 26) for c in uuid.uuid4().hex[:8])
    return "Framescope" + letters


def _add_allergy_as_the_bot(fx: Any, pid: str, actor: str) -> None:
    """An allergy for the saved patient, logged as OpenEMR logs the bot's
    own writes: under its user and the patient's pid."""

    title = f"Synthetic allergy {uuid.uuid4().hex[:6]}"
    statement = (
        "INSERT INTO lists (date, type, title, pid, activity) "
        f"VALUES (NOW(), 'allergy', {_quote(title)}, {int(pid)}, 1)"
    )
    _sql(fx, statement.replace("INSERT INTO lists", f"INSERT INTO {DB}.lists"))
    comments = base64.b64encode(statement.encode("utf-8")).decode("ascii")
    _sql(
        fx,
        f"INSERT INTO {DB}.log (date, event, category, user, groupname, comments, "
        "patient_id, success) VALUES (NOW(), 'patient-record-insert', "
        f"'Patient Demographics', {_quote(actor)}, 'Default', {_quote(comments)}, "
        f"{int(pid)}, 1)",
    )


def _save_and_judge(
    fx: Any, *, allergy: bool
) -> tuple[FrameVerdict, FrameVerdict, list[dict[str, Any]]]:
    """Save a patient through the API, optionally add the bot's allergy for
    it, and judge the window with and without ``frame.database``."""

    actor_name = _actor(fx)
    lname = _lname()
    params = {"lname": lname, "email": f"{lname.lower()}@example.invalid"}
    before_patients, before_lists = _patients(fx), _lists(fx)
    since = _last_log_id(fx)

    response = fx.token_session("actor").post(
        f"{fx.api_base_url}/apis/default/api/patient",
        json={
            "title": "Ms.",
            "fname": "Ada",
            "lname": lname,
            "DOB": "1984-03-07",
            "sex": "Female",
            "street": "101 SYNTHETIC WAY",
            "city": "EXAMPLETON",
            "state": "MA",
            "postal_code": "02139",
            "phone_home": "555-0107",
            "email": params["email"],
            "country_code": "USA",
        },
    )
    assert response.status_code == 201, (
        f"OpenEMR refused the patient write: HTTP {response.status_code}"
    )
    saved = _lines(
        fx, f"SELECT pid FROM {DB}.patient_data WHERE lname = {_quote(lname)}"
    )
    assert len(saved) == 1, "the bot's save did not land exactly once"
    if allergy:
        _add_allergy_as_the_bot(fx, saved[0], actor_name)

    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"lname": ValueExpr(param="lname"), "email": ValueExpr(param="email")},
    ).resolve(params)
    window = _window(fx, since)
    users = {r[1]: r[0] for r in _rows(fx, f"SELECT id, username FROM {DB}.users")}

    def judge(whole_database: bool) -> FrameVerdict:
        surfaces: dict[str, Any] = {
            "patient": (
                EffectState(substrate="sql", reachable=True, records=before_patients),
                _patients(fx),
            )
        }
        if whole_database:
            surfaces["lists"] = (
                EffectState(substrate="sql", reachable=True, records=before_lists),
                _lists(fx),
            )
        return judge_frame(
            _spec(actor_name, whole_database=whole_database),
            [effect],
            surfaces,
            params=params,
            routes={"0": ["patient"]},
            audit_before=[],
            audit_after=window,
            user_ids=users,
        )

    return judge(False), judge(True), window


def _tables(window: list[dict[str, Any]], actor: str) -> set[str]:
    return {e["tbl"] for e in window if e["user"] == actor}


def test_without_the_whole_database_an_audit_frame_never_holds(openemr: Any) -> None:
    unread, _whole, window = _save_and_judge(openemr, allergy=True)
    assert "lists" in _tables(window, _actor(openemr)), (
        "the bot's allergy entry is not in the window"
    )
    assert unread.status == "not_checked"
    assert not unread.held
    assert "frame.database" in unread.reason


def test_the_bots_allergy_for_the_saved_patient_refutes(openemr: Any) -> None:
    _unread, whole, _window = _save_and_judge(openemr, allergy=True)
    assert whole.status == "refuted", whole.reason


def test_the_save_alone_holds_with_the_whole_database(openemr: Any) -> None:
    """Control: the same frame over the same tables holds for a clean save,
    so the refutation above is the allergy's."""

    unread, whole, _window = _save_and_judge(openemr, allergy=False)
    assert unread.status == "not_checked"
    assert whole.status == "held", whole.reason
