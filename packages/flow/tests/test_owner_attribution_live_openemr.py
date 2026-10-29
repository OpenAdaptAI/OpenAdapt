"""Live test of owner-scoped attribution on a real OpenEMR.

While the bot saves a made-up patient through OpenEMR's Standard REST API (the
fixture's ``actor`` OAuth client), a second real OpenEMR user, signed in to the
OpenEMR web UI in their own browser session, changes one of their own UI
settings. OpenEMR writes that setting to ``user_settings`` (owned by
``setting_user``) and, the first time anyone saves the label, the label's
global default beside it (``setting_user = 0``), and logs the writes under
that user. The frame must attribute both rows to them and the save must
verify. Two controls change
the second user's setting as the bot instead, and the frame must refute.

What is real and what is not:

- The second user's setting change goes through OpenEMR itself: their UI
  session posts to ``library/ajax/user_settings.php``, the endpoint the
  chart's collapsible sections use, with the session's CSRF token. OpenEMR
  writes the row and its own audit log entry.
- The second user is created with SQL before the baseline (a copy of the
  actor's ``users``, ``users_secure``, ``groups`` and ACL rows, so the same
  password signs in). OpenEMR has no API for creating users.
- OpenEMR logs a ``user_settings`` write as an ``other`` event, which it
  records only when the ``audit_events_other`` global is on. The module turns
  it on before the baseline and restores the previous value afterwards.
- In the controls, the bot's change to the second user's row is SQL, because
  OpenEMR lets a user change only their own settings. In the first, the
  second user sets their own setting through OpenEMR and the bot then changes
  that same row, adding the audit row OpenEMR would write for it (event
  ``other-update`` under the bot's user name, the statement in ``comments``)
  filed under the chart it just saved. Only the rule that the bot's own entry
  for a table makes every row of it the bot's refutes that. The second adds
  no audit row at all.
- The fixture's database isn't reachable from the host, so this module reads
  it with ``SELECT`` statements through the fixture's own ``docker compose
  exec`` channel and calls the frame judge (``judge_frame``) on the result,
  rather than running ``verify-run``'s whole-database snapshot. The slice is
  ``patient_data`` and ``user_settings``, and the audit read is the window's
  write entries that name ``user_settings`` (the table under test; the
  record-keyed path for patient entries is unchanged and tested elsewhere).

The module is skipped unless ``OPENEMR_OWNER_ATTRIBUTION_LIVE=1``. It needs
the local fixture already up and bootstrapped (``benchmark/openemr_local``;
it does not start containers) and Playwright's Chromium. Run it from
``packages/flow``::

    OPENEMR_OWNER_ATTRIBUTION_LIVE=1 PYTHONPATH=../types:. \\
        python -m pytest tests/test_owner_attribution_live_openemr.py -v -p no:cacheprovider

Optional environment, as for ``tests/test_composite_key_live_openemr.py``:
``OPENEMR_FIXTURE_STATE``, ``OPENEMR_FIXTURE_LOCK``, ``OPENEMR_API_URL``,
``OPENEMR_UI_URL``, and ``OPENEMR_OWNER_ATTRIBUTION_RESET=1`` to restore the
fixture's baseline database afterwards (only for the session that holds the
fixture). Without a reset, the second user and the made-up patients stay.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from openadapt_flow.app_profiles import load_profile
from openadapt_flow.runtime.effects import Effect, EffectKind, EffectState, ValueExpr
from openadapt_flow.runtime.effects.frame import FrameSpec, FrameVerdict, judge_frame

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENEMR_OWNER_ATTRIBUTION_LIVE") != "1",
    reason=(
        "live OpenEMR: set OPENEMR_OWNER_ATTRIBUTION_LIVE=1 with the local "
        "fixture up (benchmark/openemr_local); see this module's docstring"
    ),
)

SECOND_USER = "oaowner2"
DB = "openemr"
#: The statement's table, from OpenEMR's logged SQL text.
_TABLE = re.compile(
    r"^\s*(?:insert\s+(?:ignore\s+)?into|replace\s+into|update|delete\s+from)\s+"
    r"`?(?:\w+`?\.`?)?(\w+)`?",
    re.IGNORECASE,
)


@pytest.fixture(scope="module")
def openemr() -> Iterator[Any]:
    from benchmark.openemr_local.qualify_hooks import fixture

    fx = fixture()
    previous = _lines(
        fx,
        f"SELECT gl_value FROM {DB}.globals WHERE gl_name = 'audit_events_other'",
    )
    _sql(
        fx,
        f"INSERT INTO {DB}.globals (gl_name, gl_index, gl_value) "
        "VALUES ('audit_events_other', 0, '1') "
        "ON DUPLICATE KEY UPDATE gl_value = '1'",
    )
    _ensure_second_user(fx)
    yield fx
    if os.environ.get("OPENEMR_OWNER_ATTRIBUTION_RESET") == "1":
        fx.reset()
    elif previous:
        _sql(
            fx,
            f"UPDATE {DB}.globals SET gl_value = {_quote(previous[0])} "
            "WHERE gl_name = 'audit_events_other'",
        )
    else:
        _sql(fx, f"DELETE FROM {DB}.globals WHERE gl_name = 'audit_events_other'")


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


def _ensure_second_user(fx: Any) -> None:
    """A second OpenEMR user with the actor's password and access."""

    if _lines(fx, f"SELECT id FROM {DB}.users WHERE username = {_quote(SECOND_USER)}"):
        return
    actor = _quote(_actor(fx))
    name = _quote(SECOND_USER)
    _sql(
        fx,
        f"""
        USE {DB};
        CREATE TEMPORARY TABLE oa_u AS SELECT * FROM {DB}.users WHERE username = {actor};
        UPDATE oa_u SET id = (SELECT MAX(id) + 1 FROM {DB}.users), username = {name},
          fname = 'Owner', lname = 'Second', uuid = NULL;
        INSERT INTO {DB}.users SELECT * FROM oa_u;
        CREATE TEMPORARY TABLE oa_s AS
          SELECT * FROM {DB}.users_secure WHERE username = {actor};
        UPDATE oa_s SET id = (SELECT id FROM oa_u), username = {name};
        INSERT INTO {DB}.users_secure SELECT * FROM oa_s;
        INSERT INTO {DB}.`groups` (name, user)
          SELECT name, {name} FROM {DB}.`groups` WHERE user = {actor};
        UPDATE {DB}.gacl_aro_seq SET id = id + 1;
        INSERT INTO {DB}.gacl_aro (id, section_value, value, order_value, name, hidden)
          SELECT (SELECT id FROM {DB}.gacl_aro_seq), 'users', {name}, 10,
            'Second Owner', 0;
        INSERT INTO {DB}.gacl_groups_aro_map (group_id, aro_id)
          SELECT m.group_id, (SELECT id FROM {DB}.gacl_aro_seq)
          FROM {DB}.gacl_groups_aro_map m JOIN {DB}.gacl_aro a ON a.id = m.aro_id
          WHERE a.section_value = 'users' AND a.value = {actor};
        """,
    )


class _SecondUser:
    """The second user's own OpenEMR web session."""

    def __init__(self, fx: Any) -> None:
        from playwright.sync_api import sync_playwright

        values = fx._runtime_values()
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self.page = self._browser.new_page()
        self.page.goto(fx.ui_base_url)
        self.page.locator("#authUser").fill(SECOND_USER)
        self.page.locator("#clearPass").fill(values["OPENEMR_ACTOR_PASSWORD"])
        self.page.locator("#login-button").click()
        self.page.wait_for_url("**/interface/main/tabs/main.php**", timeout=60_000)
        self.page.wait_for_function(
            "() => typeof (window.csrf_token_js || top.csrf_token_js) === 'string'",
            timeout=60_000,
        )

    def set_own_setting(self, target: str, mode: int) -> None:
        """What a chart's collapsible section posts when it is toggled."""

        status = self.page.evaluate(
            """async ({target, mode}) => {
                const body = new URLSearchParams({
                    csrf_token_form: window.csrf_token_js || top.csrf_token_js,
                    target: target,
                    mode: String(mode),
                });
                const r = await fetch('/library/ajax/user_settings.php', {
                    method: 'POST', body: body, credentials: 'same-origin',
                });
                return r.status;
            }""",
            {"target": target, "mode": mode},
        )
        assert status == 200, f"user_settings.php returned HTTP {status}"

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()


@pytest.fixture()
def second_user(openemr: Any) -> Iterator[_SecondUser]:
    session = _SecondUser(openemr)  # signed in before the baseline
    yield session
    session.close()


def _user_id(fx: Any, username: str) -> str:
    found = _lines(fx, f"SELECT id FROM {DB}.users WHERE username = {_quote(username)}")
    assert found, f"no OpenEMR user {username!r}"
    return found[0]


def _patients(fx: Any) -> list[dict[str, Any]]:
    return [
        {"id": r[0], "pid": r[1], "fname": r[2], "lname": r[3], "email": r[4]}
        for r in _rows(
            fx, f"SELECT id, pid, fname, lname, email FROM {DB}.patient_data"
        )
    ]


def _settings(fx: Any) -> list[dict[str, Any]]:
    return [
        {
            "id": f"{r[0]}:{r[1]}",
            "setting_user": r[0],
            "setting_label": r[1],
            "setting_value": bytes.fromhex(r[2]).decode("utf-8", "replace"),
        }
        for r in _rows(
            fx,
            "SELECT setting_user, setting_label, HEX(setting_value) "
            f"FROM {DB}.user_settings",
        )
    ]


def _last_log_id(fx: Any) -> int:
    found = _lines(fx, f"SELECT IFNULL(MAX(id), 0) FROM {DB}.log")
    return int(found[0])


def _statement(comments: bytes) -> str:
    """The logged statement, plain or as OpenEMR 8 stores it (base64)."""

    plain = comments.decode("utf-8", "replace")
    if _TABLE.match(plain):
        return plain
    # The read is cut at 300 bytes; decode whole base64 groups only.
    cut = comments[: len(comments) - len(comments) % 4]
    try:
        return base64.b64decode(cut, validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return plain


def _window(fx: Any, after_id: int) -> list[dict[str, Any]]:
    """The window's write entries that name ``user_settings``.

    OpenEMR's log has no table column: the table is read from the logged
    statement. An entry whose statement is encrypted names no table and is
    left out, so it can explain nothing.
    """

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
        if kind not in ("insert", "update", "delete") or encrypted == "Yes":
            continue
        match = _TABLE.match(_statement(bytes.fromhex(comments_hex)))
        if match is None or match.group(1).lower() != "user_settings":
            continue
        out.append(
            {
                "id": log_id,
                "user": user,
                "patient_id": patient_id or None,
                "kind": kind,
                "tbl": "user_settings",
            }
        )
    return out


def _spec(actor: str) -> FrameSpec:
    profile = load_profile("openemr")
    return FrameSpec.model_validate(
        {
            "identity_field": "id",
            "attribution": {
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
            },
            "database": {
                "dialect": "mysql",
                # The judge reads nothing; this module passes the rows.
                "read": {"sql_driver": "pymysql"},
                "profile": "openemr",
                "profile_sha256": profile.sha256(),
                # Each surface is its table: user_settings' owner comes from
                # the profile's own_rows rule.
                "views": {"patient_data": "patient", "user_settings": "user_settings"},
            },
        }
    )


def _lname() -> str:
    letters = "".join(chr(ord("a") + int(c, 16) % 26) for c in uuid.uuid4().hex[:8])
    return "Ownerattr" + letters


def _save_and_judge(fx: Any, during: Callable[[str], None]) -> FrameVerdict:
    """The bot saves a patient through the API, then ``during`` runs with
    the saved chart's pid; the frame is judged over patient_data and
    user_settings."""

    actor_name = _actor(fx)
    lname = _lname()
    params = {"lname": lname, "email": f"{lname.lower()}@example.invalid"}
    before_patients, before_settings = _patients(fx), _settings(fx)
    since = _last_log_id(fx)

    actor = fx.token_session("actor")
    response = actor.post(
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
    during(saved[0])

    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"lname": ValueExpr(param="lname"), "email": ValueExpr(param="email")},
    ).resolve(params)
    users = {r[1]: r[0] for r in _rows(fx, f"SELECT id, username FROM {DB}.users")}
    return judge_frame(
        _spec(actor_name),
        [effect],
        {
            "patient": (
                EffectState(substrate="sql", reachable=True, records=before_patients),
                _patients(fx),
            ),
            "user_settings": (
                EffectState(substrate="sql", reachable=True, records=before_settings),
                _settings(fx),
            ),
        },
        params=params,
        routes={"0": ["patient"]},
        audit_before=[],
        audit_after=_window(fx, since),
        user_ids=users,
    )


def _target() -> str:
    # user_settings.php accepts only [A-Za-z0-9_] labels ending in _expand.
    return f"oa{uuid.uuid4().hex[:8]}_expand"


def test_the_second_users_own_setting_is_theirs(
    openemr: Any, second_user: _SecondUser
) -> None:
    target = _target()
    verdict = _save_and_judge(
        openemr, lambda _pid: second_user.set_own_setting(target, 1)
    )
    owner = _user_id(openemr, SECOND_USER)
    rows = [r for r in _settings(openemr) if r["setting_label"] == target]
    # OpenEMR writes two rows: the user's own, and, since nobody has saved
    # this label before, the global default (setting_user = 0) that
    # setUserSetting(..., createDefault=true) adds beside it.
    assert sorted(r["setting_user"] for r in rows) == sorted(["0", owner]), (
        "OpenEMR did not write the second user's setting and its default"
    )
    assert verdict.status == "held", verdict.reason
    # Both are theirs: their own row by their entry, the default beside it.
    assert sorted(c.key for c in verdict.concurrent) == sorted(
        [f"0:{target}", f"{owner}:{target}"]
    ), verdict.reason


def test_the_bot_changing_the_second_users_setting_refutes(
    openemr: Any, second_user: _SecondUser
) -> None:
    owner = _user_id(openemr, SECOND_USER)
    target = _target()

    def during(pid: str) -> None:
        # The second user sets their own setting through OpenEMR (their
        # entry for user_settings; OpenEMR adds the label's default row
        # beside it), then the bot changes the second user's row. That row
        # is the second user's, with the second user's entry: only the rule
        # that the bot's own entry for the table makes every row of it the
        # bot's can refute it, and then the default row has no explained
        # row beside it. OpenEMR lets a user write only their own
        # settings, so the bot's write is SQL, with the audit row OpenEMR
        # would write, filed under the chart the bot just saved (so the
        # audit-only check passes it).
        second_user.set_own_setting(target, 1)
        statement = (
            f"UPDATE user_settings SET setting_value = '0' "
            f"WHERE setting_user = {owner} AND setting_label = '{target}'"
        )
        _sql(
            openemr,
            f"UPDATE {DB}.user_settings SET setting_value = '0' "
            f"WHERE setting_user = {owner} AND setting_label = {_quote(target)};"
            f"INSERT INTO {DB}.log (date, event, user, groupname, comments, "
            "patient_id, success) VALUES (NOW(), 'other-update', "
            f"{_quote(_actor(openemr))}, 'Default', {_quote(statement)}, "
            f"{int(pid)}, 1);",
        )

    verdict = _save_and_judge(openemr, during)
    assert verdict.status == "refuted", verdict.reason
    # Refuted by that rule: the second user's row is the bot's, the default
    # beside it unexplained, and no audit-only entry.
    assert sorted(
        (c.surface, str(c.key), c.attribution, c.audit_only) for c in verdict.collateral
    ) == sorted(
        [
            ("user_settings", f"{owner}:{target}", "self", False),
            ("user_settings", f"0:{target}", "unattributed", False),
        ]
    ), verdict.reason
    assert verdict.concurrent == [], verdict.reason


def test_an_unlogged_change_to_the_second_users_setting_refutes(
    openemr: Any,
) -> None:
    owner = _user_id(openemr, SECOND_USER)
    target = _target()
    verdict = _save_and_judge(
        openemr,
        lambda _pid: _sql(
            openemr,
            f"INSERT INTO {DB}.user_settings (setting_user, setting_label, "
            f"setting_value) VALUES ({owner}, {_quote(target)}, '1')",
        ),
    )
    assert verdict.status == "refuted", verdict.reason
    assert [(c.surface, c.attribution) for c in verdict.collateral] == [
        ("user_settings", "unattributed")
    ], verdict.reason
