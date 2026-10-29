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


@pytest.fixture(scope="module")
def openemr() -> Any:
    from benchmark.openemr_local.qualify_hooks import fixture

    fx = fixture()
    yield fx
    if os.environ.get("OPENEMR_LATE_WRITE_RESET") == "1":
        fx.reset()


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
    frame: dict[str, Any] = {
        "database": {
            "dialect": "mysql",
            "read": {
                "sql_driver": os.environ.get("OPENEMR_DB_DRIVER", "pymysql"),
                "sql_connect_args": {
                    "host": os.environ.get("OPENEMR_DB_HOST", "127.0.0.1"),
                    "port": int(os.environ.get("OPENEMR_DB_PORT", "3306")),
                    "user": os.environ["OPENEMR_DB_USER"],
                    "database": os.environ.get("OPENEMR_DB_NAME", "openemr"),
                },
                "sql_password_env": "OPENEMR_DB_PASSWORD",
            },
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
                ]
            },
            "recheck_after_s": float(
                os.environ.get("OPENEMR_LATE_WRITE_RECHECK_S", "2")
            ),
        }
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
                "base_url": fx.api_base_url,
                "records_path": f"/apis/default/api/patient?lname={lname}&_limit=100",
                "records_key": "data",
                "session": "benchmark.openemr_local.qualify_hooks:oracle_session",
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
        f"SELECT pid FROM openemr.patient_data WHERE email = {fx._sql_quote(email)}"
    )
    assert len(rows) == 1, f"expected one chart for the made-up email, got {len(rows)}"
    return int(rows[0])


def test_late_collateral_after_a_verified_save_is_refuted(openemr: Any) -> None:
    lname = _lname()
    actor = openemr.token_session("actor")
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
