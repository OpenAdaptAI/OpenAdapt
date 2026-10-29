"""Live test of the late-write recheck (``frame.database.recheck_after_s``) on
a real OpenEMR.

The whole-database frame reads the database twice after a save, ``settle_s``
apart. A write that lands later lands after the verdict. This module saves
made-up patients through OpenEMR's Standard REST API (the fixture's
``actor`` OAuth client), verifies each save with ``openadapt_flow.verifier``
(the record read back with the read-only ``oracle`` client, the frame over
the whole MariaDB database through a read-only account), and then:

1. **late collateral:** after the first patient's save is VERIFIED, a direct
   SQL ``UPDATE`` changes another chart (one this test made before the
   save). The recheck must turn the verdict into REFUTED (late).
2. **clean control:** after the first patient's save is VERIFIED, only a
   second, verified save happens. The recheck, given the second save as
   ``excused_by``, must stay CONFIRMED.

Each run uses a fresh random last name, so it needs no reset. The patients
stay in the database until the fixture is reset.

The module is skipped unless ``OPENEMR_LATE_WRITE_LIVE=1``. It needs the local
fixture up and bootstrapped (``benchmark/openemr_local``; it does not start
containers), a DB-API driver for MariaDB (``pymysql`` by default), and a
read-only database account the test can reach (``SELECT`` on the ``openemr``
schema and ``TRIGGER``, so the frame can see triggers). Run it from
``packages/flow``::

    OPENEMR_LATE_WRITE_LIVE=1 OPENEMR_DB_USER=frame_reader \\
    OPENEMR_DB_PASSWORD=... OPENEMR_DB_PORT=3306 PYTHONPATH=../types:. \\
        python -m pytest tests/test_late_write_recheck_live_openemr.py -v \\
        -p no:cacheprovider

Environment:

- ``OPENEMR_DB_HOST`` (default ``127.0.0.1``), ``OPENEMR_DB_PORT`` (default
  ``3306``), ``OPENEMR_DB_USER`` (required), ``OPENEMR_DB_PASSWORD``
  (required; read through ``sql_password_env``), ``OPENEMR_DB_NAME``
  (default ``openemr``), ``OPENEMR_DB_DRIVER`` (default ``pymysql``);
- ``OPENEMR_LATE_WRITE_RECHECK_S``: the test's ``recheck_after_s`` (default
  2). A test value only: the right wait for a deployment depends on the
  app's own jobs, and the contract's author picks it;
- ``OPENEMR_LATE_WRITE_FRAME_JSON``: a JSON object merged into the
  contract's ``frame`` (for example ``new_row_fields`` or ``side_records``,
  if this OpenEMR build's patient insert sets columns the default doesn't
  list), so the first save can be tuned to VERIFIED without editing the test;
- as for ``test_composite_key_live_openemr.py``: ``OPENEMR_FIXTURE_STATE``,
  ``OPENEMR_FIXTURE_LOCK``, ``OPENEMR_API_URL``;
- ``OPENEMR_LATE_WRITE_RESET=1``: restore the fixture's baseline database
  afterwards (only for the session that holds the fixture).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENEMR_LATE_WRITE_LIVE") != "1",
    reason=(
        "live OpenEMR: set OPENEMR_LATE_WRITE_LIVE=1 with the local fixture up "
        "(benchmark/openemr_local) and a read-only database account; see this "
        "module's docstring"
    ),
)

#: Who writes: the fixture's actor OAuth client, through this test.
ACTOR_ID = "openemr-late-write-live"
FIELDS = (
    "title",
    "fname",
    "lname",
    "DOB",
    "sex",
    "street",
    "city",
    "postal_code",
    "phone_home",
    "email",
    "country_code",
)


#: OpenEMR's audit log as the frame's attribution needs it: who (``user``),
#: which chart (``patient_id``), the kind of write (``ev``: the event's last
#: part, ``insert``, ``update``, ``delete`` or a read) and the table (``tbl``:
#: the third word of the logged statement, the second for an update). Only
#: entries that name a chart: one that names none can't attribute a row, and
#: the whole-database diff still shows every row it wrote. The read-only
#: guard refuses write keywords even inside literals, so the update case is
#: told by its first letter. ``OPENEMR_LATE_WRITE_AUDIT_SQL`` replaces it.
AUDIT_SQL = (
    "SELECT id, user, patient_id, "
    "LOWER(SUBSTRING_INDEX(event, '-', -1)) AS ev, "
    "LOWER(TRIM(BOTH '`' FROM SUBSTRING_INDEX(SUBSTRING_INDEX(SUBSTRING_INDEX("
    "TRIM(comments), ' ', IF(LEFT(SUBSTRING_INDEX(event, '-', -1), 1) = 'u', "
    "2, 3)), ' ', -1), '(', 1))) AS tbl "
    "FROM log WHERE patient_id > 0"
)
#: Where the test puts the oracle client's bearer token for the REST read.
#: verify-run drops a ``verifier.session`` hook (a qualify-only setting, so
#: the read went out unauthenticated: the 401), so the read uses
#: ``auth.bearer_env``.
TOKEN_ENV = "OPENEMR_LATE_WRITE_ORACLE_TOKEN"


def _email_literal(email: str) -> str:
    """The test's own made-up email as a SQL literal (the fixture's quoting
    refuses ``@``)."""

    import re

    if not re.fullmatch(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+", email):
        raise ValueError("unexpected email")
    return "'" + email + "'"


@pytest.fixture(scope="module")
def openemr() -> Any:
    from benchmark.openemr_local.qualify_hooks import fixture

    fx = fixture()
    yield fx
    if os.environ.get("OPENEMR_LATE_WRITE_RESET") == "1":
        fx.reset()


@pytest.fixture(scope="module", autouse=True)
def _oracle_token(openemr: Any) -> Any:
    """The read-only oracle client's token, for the REST read; issued once,
    before any baseline, so its OAuth bookkeeping isn't in a window."""

    if os.environ.get("OPENEMR_LATE_WRITE_BEARER_ENV"):
        yield
        return
    session = openemr.token_session("oracle")
    token = str(session.headers["Authorization"]).split(" ", 1)[1]
    os.environ[TOKEN_ENV] = token
    yield
    os.environ.pop(TOKEN_ENV, None)


def _warm(fx: Any, actor: Any) -> None:
    """Use both tokens once, then wait: OpenEMR writes the OAuth bookkeeping
    after the token response, and it must land before the baseline."""

    response = actor.get(f"{fx.api_base_url}/apis/default/api/patient?_limit=1")
    assert response.status_code == 200, f"actor read: HTTP {response.status_code}"
    time.sleep(2)


def _actor_user(fx: Any) -> str:
    """The OpenEMR user both OAuth clients sign in as: the bot's own id in
    the audit log."""

    return os.environ.get("OPENEMR_LATE_WRITE_SELF") or str(
        fx._runtime_values()["OPENEMR_ACTOR_USER"]
    )


@pytest.fixture(autouse=True)
def _state_dir(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from openadapt_flow import verifier

    monkeypatch.setattr(verifier, "USED_DIR", tmp_path / "verifier-state")
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))


def _lname() -> str:
    letters = "".join(chr(ord("a") + int(c, 16) % 26) for c in uuid.uuid4().hex[:8])
    return "Latewrite" + letters


def _patient(lname: str, n: int) -> dict[str, str]:
    return {
        "title": "Ms.",
        "fname": f"Ada{chr(ord('a') + n)}",
        "lname": lname,
        "DOB": f"198{n}-03-07",
        "sex": "Female",
        "street": "101 SYNTHETIC WAY",
        "city": "EXAMPLETON",
        "state": "MA",
        "postal_code": "02139",
        "phone_home": f"555-010{n}",
        "email": f"{lname.lower()}.{n}@example.invalid",
        "country_code": "USA",
    }


def _contract(fx: Any, lname: str) -> Any:
    from openadapt_flow.app_profiles import load_profile
    from openadapt_flow.verifier import Contract

    if not os.environ.get("OPENEMR_DB_USER") or not os.environ.get(
        "OPENEMR_DB_PASSWORD"
    ):
        pytest.fail("set OPENEMR_DB_USER and OPENEMR_DB_PASSWORD (read-only account)")
    read = {
        "sql_driver": os.environ.get("OPENEMR_DB_DRIVER", "pymysql"),
        "sql_connect_args": {
            "host": os.environ.get("OPENEMR_DB_HOST", "127.0.0.1"),
            "port": int(os.environ.get("OPENEMR_DB_PORT", "3306")),
            "user": os.environ["OPENEMR_DB_USER"],
            "database": os.environ.get("OPENEMR_DB_NAME", "openemr"),
        },
        "sql_password_env": "OPENEMR_DB_PASSWORD",
    }
    frame: dict[str, Any] = {
        "identity_field": "pid",
        # OpenEMR's patient insert writes one history_data row for the chart.
        "side_records": [
            {
                "surface": "history_data",
                "link_field": "pid",
                "of_surface": "patient_data",
                "of_field": "pid",
            }
        ],
        # Other users' writes, and the bot's own per-user bookkeeping (the
        # profile's own-row rules need its sign-in name), through OpenEMR's
        # audit log, read on the frame's own read-only connection.
        "attribution": {
            "kind": "audit_read",
            "actor_field": "user",
            "self": [_actor_user(fx)],
            "record_field": "patient_id",
            "record_key": "pid",
            "event_field": "ev",
            "write_events": ["insert", "update", "delete"],
            "insert_events": ["insert"],
            "delete_events": ["delete"],
            "table_field": "tbl",
            "read": {
                "kind": "sql",
                **read,
                "sql_query": os.environ.get("OPENEMR_LATE_WRITE_AUDIT_SQL", AUDIT_SQL),
            },
        },
        "database": {
            "dialect": "mysql",
            "read": dict(read),
            "profile": "openemr",
            "profile_sha256": load_profile("openemr").sha256(),
            # The REST read set is one surface (""): the chart rows it shows.
            "views": {"patient_data": ""},
            # What OpenEMR's patient insert fills on its own.
            "new_row_fields": {
                "patient_data": [
                    "pid",
                    "pubpid",
                    "uuid",
                    "date",
                    "regdate",
                    "created_by",
                    "updated_by",
                    "last_updated",
                    "state",
                ],
                "history_data": ["id", "pid", "uuid", "date", "created_by"],
            },
            "recheck_after_s": float(
                os.environ.get("OPENEMR_LATE_WRITE_RECHECK_S", "2")
            ),
        },
    }
    extra = os.environ.get("OPENEMR_LATE_WRITE_FRAME_JSON")
    if extra:
        merged = json.loads(extra)
        database = {**frame["database"], **merged.pop("database", {})}
        frame = {**frame, **merged, "database": database}
    return Contract.from_dict(
        {
            "schema_version": "openadapt.verify/v1",
            "environment_id": "openemr-local-fixture-late-write",
            "minimum_effect_strength": "independent_system_of_record",
            "verifier": {
                "kind": "rest",
                # The fixture's HTTPS port has a self-signed certificate,
                # which the verifier rightly refuses: OPENEMR_LATE_WRITE_API_URL
                # can point at its plain-HTTP port instead.
                "base_url": os.environ.get(
                    "OPENEMR_LATE_WRITE_API_URL", fx.ui_base_url
                ),
                "records_path": f"/apis/default/api/patient?lname={lname}&_limit=100",
                "records_key": "data",
                "auth": {
                    "bearer_env": os.environ.get(
                        "OPENEMR_LATE_WRITE_BEARER_ENV", TOKEN_ENV
                    )
                },
            },
            "record": {
                "name": "patient",
                "match": ["email", "lname"],
                "duplicate_key": "email",
                "fields": {name: name for name in FIELDS},
            },
            "frame": frame,
        }
    )


def _create(fx: Any, actor: Any, body: dict[str, str]) -> None:
    response = actor.post(f"{fx.api_base_url}/apis/default/api/patient", json=body)
    assert response.status_code == 201, (
        f"OpenEMR refused the patient write: HTTP {response.status_code}"
    )


def _verified_save(fx: Any, actor: Any, contract: Any, body: dict[str, str]) -> Any:
    from openadapt_flow.verifier import Actor, begin_write, verify_write

    params = {name: body[name] for name in FIELDS}
    time.sleep(2)  # the previous request's bookkeeping lands first
    pending = begin_write(contract, params, actor=Actor("script", ACTOR_ID))
    _create(fx, actor, body)
    return verify_write(pending)


def _recheck(contract: Any, result: Any, body: dict[str, str], **kw: Any) -> Any:
    from openadapt_flow.verifier import Actor, recheck

    params = {name: body[name] for name in FIELDS}
    return recheck(
        contract,
        result,
        params=params,
        actor=Actor("script", ACTOR_ID),
        wait=True,
        **kw,
    )


def _pid(fx: Any, email: str) -> int:
    rows = fx._db_lines(
        f"SELECT pid FROM openemr.patient_data WHERE email = {_email_literal(email)}"
    )
    assert len(rows) == 1, f"expected one chart for the made-up email, got {len(rows)}"
    return int(rows[0])


def test_late_collateral_after_a_verified_save_is_refuted(openemr: Any) -> None:
    lname = _lname()
    actor = openemr.token_session("actor")
    _warm(openemr, actor)
    contract = _contract(openemr, lname)

    other = _patient(lname, 1)  # another chart, made before the save
    _create(openemr, actor, other)
    other_pid = _pid(openemr, other["email"])

    first = _patient(lname, 2)
    result = _verified_save(openemr, actor, contract, first)
    assert result.verified, (result.reasons, result.evidence.get("frame"))
    assert result.recheck_due_at == result.receipt["recheck_due_at"]

    # After the verdict: a change to another chart that no write declares,
    # made straight in the database (a job or an integration would do this).
    openemr._db(
        "UPDATE openemr.patient_data SET phone_cell = '555-0199' "
        f"WHERE pid = {other_pid}"
    )
    late = _recheck(contract, result, first)
    assert late.outcome == "REFUTED", (late.reasons, late.evidence)
    assert late.late and late.evidence["collateral_count"] >= 1


def test_only_a_later_verified_save_does_not_refute(openemr: Any) -> None:
    lname = _lname()
    actor = openemr.token_session("actor")
    _warm(openemr, actor)
    contract = _contract(openemr, lname)

    first = _patient(lname, 2)
    result = _verified_save(openemr, actor, contract, first)
    assert result.verified, (result.reasons, result.evidence.get("frame"))

    second = _patient(lname, 3)
    later = _verified_save(openemr, actor, contract, second)
    assert later.verified, (later.reasons, later.evidence.get("frame"))

    late = _recheck(contract, result, first, excused_by=[later])
    assert late.outcome == "CONFIRMED", (late.reasons, late.evidence)
    assert late.excused == 1
