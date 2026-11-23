"""``NULL`` versus ``""`` on a real app's first form save (#2391), lab only.

The real-app benchmark saw OpenEMR's demographics form write ``""`` back for
optional fields that were ``NULL``, so the judge called every correct GUI
save a clobber. This test reproduces that on a local OpenEMR 7.0.3 with a
headless browser, then checks the general fix: the change is learned per
field as a ``null_empty`` codec, the correct save passes, and a real clobber
on the same save is still caught.

Set ``OAFLOW_TX_LIVE_OPENEMR_URL`` (for example ``http://127.0.0.1:9746``) and
``OAFLOW_TX_LIVE_OPENEMR_SQL`` (a command that runs SQL from stdin against the
app's database, for example ``docker exec -i <db> mariadb -uopenemr
-popenemr openemr``). The sign-in defaults are the fixture account from
``benchmark/openemr_live/docker-compose.yml``. Synthetic records only; the
browser is headless.
"""

from __future__ import annotations

import os
import secrets
import shlex
import subprocess
from typing import Any

import pytest

URL = os.environ.get("OAFLOW_TX_LIVE_OPENEMR_URL", "").strip().rstrip("/")
SQL = os.environ.get("OAFLOW_TX_LIVE_OPENEMR_SQL", "").strip()
USER = os.environ.get("OAFLOW_TX_LIVE_OPENEMR_USER", "admin")
PASS = os.environ.get("OAFLOW_TX_LIVE_OPENEMR_PASS", "pass")

pytestmark = pytest.mark.skipif(
    not (URL and SQL), reason="set OAFLOW_TX_LIVE_OPENEMR_URL and _SQL"
)

#: Columns the test reads. ``phone_home`` is the field the save intends to
#: change; the rest are optional demographics left NULL at seed time.
COLUMNS = (
    "pid",
    "fname",
    "lname",
    "DOB",
    "phone_home",
    "phone_cell",
    "email",
    "street",
    "city",
    "postal_code",
    "occupation",
)


def _sql(statement: str) -> str:
    done = subprocess.run(
        shlex.split(SQL), input=statement, capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr[-400:]
    return done.stdout


def _row(pid: int) -> dict[str, Any]:
    out = _sql(
        "SELECT "
        + ", ".join(f"IFNULL(CONCAT('=', {c}), '<NULL>') AS {c}" for c in COLUMNS)
        + f" FROM patient_data WHERE pid = {pid};"
    )
    header, values = out.strip().splitlines()[:2]
    row: dict[str, Any] = {}
    for name, raw in zip(header.split("\t"), values.split("\t")):
        row[name] = None if raw == "<NULL>" else raw[1:]
    return row


def _seed() -> int:
    tag = secrets.token_hex(3)
    out = _sql(
        "SET @p := (SELECT COALESCE(MAX(pid),0)+1 FROM patient_data); "
        "INSERT INTO patient_data (pid, pubpid, fname, lname, DOB, phone_home, "
        "uuid, date, regdate) VALUES (@p, 'LENS%s', 'Test', 'Aalens%s', "
        "'1970-01-01', '555-0100', UNHEX(REPLACE(UUID(),'-','')), NOW(), NOW()); "
        "SELECT @p;" % (tag, tag)
    )
    return int(out.strip().splitlines()[-1])


def _gui_save(pid: int, phone: str) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_context().new_page()
            page.set_default_timeout(60_000)
            page.goto(URL + "/interface/login/login.php?site=default")
            page.fill("#authUser", USER)
            page.fill("#clearPass", PASS)
            page.click("#login-button")
            page.wait_for_selector("#mainBox", state="attached")
            page.goto(
                URL + f"/interface/patient_file/summary/demographics.php?set_pid={pid}"
            )
            page.goto(URL + "/interface/patient_file/summary/demographics_full.php")
            page.wait_for_selector("input[name=form_phone_home]", state="attached")
            page.eval_on_selector(
                "input[name=form_phone_home]", "(e, v) => { e.value = v; }", phone
            )
            with page.expect_navigation(wait_until="domcontentloaded"):
                page.click("#submit_btn")
        finally:
            browser.close()


@pytest.fixture(scope="module")
def first_save() -> tuple[dict[str, Any], dict[str, Any]]:
    pid = _seed()
    try:
        before = _row(pid)
        _gui_save(pid, "555-0199")
        after = _row(pid)
    finally:
        _sql(f"DELETE FROM patient_data WHERE pid = {pid};")
    return before, after


def test_live_first_form_save_rewrites_null_to_empty(first_save) -> None:
    """The app behaviour the benchmark saw, reproduced."""
    before, after = first_save
    assert after["phone_home"] == "555-0199"
    rewritten = [
        c for c in COLUMNS if before[c] is None and after[c] == "" and c != "phone_home"
    ]
    assert rewritten, (before, after)


def test_live_judge_without_codec_calls_the_correct_save_a_clobber(first_save) -> None:
    from openadapt_flow.tx.judge import FrameAndIntentJudge

    before, after = first_save
    verdict = FrameAndIntentJudge().evaluate(before, after, {"phone_home": "555-0199"})
    assert not verdict.passed and verdict.clobbered_fields


def test_live_learned_codec_passes_the_correct_save(first_save) -> None:
    from openadapt_flow.tx.induce import learn_null_empty_codecs
    from openadapt_flow.tx.judge import FrameAndIntentJudge

    before, after = first_save
    codecs = learn_null_empty_codecs(before, after, {"phone_home"})
    assert codecs and all(c.kind == "null_empty" for c in codecs.values())
    verdict = FrameAndIntentJudge().evaluate(
        before, after, {"phone_home": "555-0199"}, field_codecs=codecs
    )
    assert verdict.passed, verdict.reasons


def test_live_learned_codec_still_catches_a_real_clobber(first_save) -> None:
    """The codec covers only the two empty spellings on the learned fields."""
    from openadapt_flow.tx.induce import learn_null_empty_codecs
    from openadapt_flow.tx.judge import FrameAndIntentJudge

    before, after = first_save
    codecs = learn_null_empty_codecs(before, after, {"phone_home"})
    learned = sorted(codecs)[0]
    clobbered = {**after, learned: "overwritten", "lname": ""}
    verdict = FrameAndIntentJudge().evaluate(
        before, clobbered, {"phone_home": "555-0199"}, field_codecs=codecs
    )
    assert not verdict.passed
    assert set(verdict.clobbered_fields) >= {learned, "lname"}
