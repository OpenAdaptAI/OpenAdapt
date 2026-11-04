"""Live test of the composite duplicate key (OpenAdapt#1393) on a real OpenEMR.

OpenEMR's own duplicate-patient check matches on last name, first name and
date of birth. This test writes made-up patients through OpenEMR's Standard
REST API (the fixture's ``actor`` OAuth client) and has the shared
``RestRecordVerifier`` read them back with the separate read-only ``oracle``
client. It checks three saves:

1. a first patient: the composite key ``{lname, fname, DOB}`` confirms;
2. a second patient with the same last and first name but another birth
   date: confirms, because the key differs;
3. a third patient with the same last name, first name and birth date as the
   first (another email): refutes, because two records now carry the key,
   one of which existed before the save.

Each run uses a fresh random last name, so it needs no reset and does not
collide with the referral fixture's patients. The patients stay in the
database until the fixture is reset.

The module is skipped unless ``OPENEMR_COMPOSITE_KEY_LIVE=1``. It needs the
local fixture already up and bootstrapped (``benchmark/openemr_local``; it
does not start containers). Run it from ``packages/flow``::

    OPENEMR_COMPOSITE_KEY_LIVE=1 PYTHONPATH=../types:. \\
        python -m pytest tests/test_composite_key_live_openemr.py -v -p no:cacheprovider

``benchmark`` here is a namespace package: an installed package with that
name (openadapt-evals installs one) shadows it, and the import of
``benchmark.openemr_local`` fails. Run from an environment without it.

Optional environment, as for ``benchmark/openemr_local/qualify_hooks.py``:

- ``OPENEMR_FIXTURE_STATE``: the fixture state directory holding the OAuth
  client ids and runtime env (default ``benchmark/openemr_local/state``);
- ``OPENEMR_FIXTURE_LOCK``: a different environment lock file;
- ``OPENEMR_API_URL``: the API base URL when not the default
  ``https://127.0.0.1:9300``;
- ``OPENEMR_COMPOSITE_KEY_RESET=1``: restore the fixture's baseline database
  afterwards (stops and starts the OpenEMR container; only for the session
  that holds the fixture).
"""

from __future__ import annotations

import os
import uuid
from typing import Any
from urllib.parse import urlencode

import pytest

from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    RestRecordVerifier,
    ValueExpr,
    Verdict,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENEMR_COMPOSITE_KEY_LIVE") != "1",
    reason=(
        "live OpenEMR: set OPENEMR_COMPOSITE_KEY_LIVE=1 with the local fixture "
        "up (benchmark/openemr_local); see this module's docstring"
    ),
)

KEY = ("lname", "fname", "DOB")


@pytest.fixture(scope="module")
def openemr() -> Any:
    from benchmark.openemr_local.qualify_hooks import fixture

    fx = fixture()
    yield fx
    if os.environ.get("OPENEMR_COMPOSITE_KEY_RESET") == "1":
        fx.reset()


def _lname() -> str:
    # Letters only: a made-up family name no fixture patient carries.
    letters = "".join(chr(ord("a") + int(c, 16) % 26) for c in uuid.uuid4().hex[:8])
    return "Dupkey" + letters


def _patient(lname: str, dob: str, email: str) -> dict[str, str]:
    return {
        "title": "Ms.",
        "fname": "Ada",
        "lname": lname,
        "DOB": dob,
        "sex": "Female",
        "street": "101 SYNTHETIC WAY",
        "city": "EXAMPLETON",
        "state": "MA",
        "postal_code": "02139",
        "phone_home": "555-0107",
        "email": email,
        "country_code": "USA",
    }


def _create(fx: Any, actor: Any, body: dict[str, str]) -> None:
    response = actor.post(f"{fx.api_base_url}/apis/default/api/patient", json=body)
    assert response.status_code == 201, (
        f"OpenEMR refused the patient write: HTTP {response.status_code}"
    )


def _effect(params: dict[str, str]) -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"lname": ValueExpr(param="lname"), "email": ValueExpr(param="email")},
        composite_key={name: ValueExpr(param=name) for name in KEY},
        count_new_only=True,
        timeout_s=10.0,
    ).resolve(params)


def test_composite_key_on_a_real_openemr(openemr: Any) -> None:
    lname = _lname()
    actor = openemr.token_session("actor")
    oracle = openemr.token_session("oracle")
    query = urlencode({"lname": lname, "_limit": "100"})
    verifier = RestRecordVerifier(
        openemr.api_base_url,
        records_path=f"/apis/default/api/patient?{query}",
        records_key="data",
        session=oracle,
        timeout_s=10.0,
        poll_interval_s=0.2,
    )

    first = _patient(lname, "1984-03-07", f"{lname.lower()}.1@example.invalid")
    before = verifier.capture_pre_state()
    assert before.reachable and before.records == []
    _create(openemr, actor, first)
    verdict = verifier.verify(_effect(first), before)
    assert verdict.verdict is Verdict.CONFIRMED, verdict.reason

    # Same last and first name, another birth date: not a duplicate.
    sibling = _patient(lname, "1990-11-21", f"{lname.lower()}.2@example.invalid")
    before = verifier.capture_pre_state()
    _create(openemr, actor, sibling)
    verdict = verifier.verify(_effect(sibling), before)
    assert verdict.verdict is Verdict.CONFIRMED, verdict.reason
    assert verdict.observed_count == 1

    # Same last name, first name and birth date as the first, another email:
    # one new record, but two records now carry the key.
    again = _patient(lname, "1984-03-07", f"{lname.lower()}.3@example.invalid")
    before = verifier.capture_pre_state()
    _create(openemr, actor, again)
    verdict = verifier.verify(_effect(again), before)
    assert verdict.verdict is Verdict.REFUTED, verdict.reason
    assert "2 records share the duplicate key (DOB, fname, lname)" in verdict.reason
    assert len(verdict.matched_records) == 2
