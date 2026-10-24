"""The same suite on the pinned OpenEMR fixture: real UI, real API, real audit log.

    python -m verification_coverage.independent.live --out DIR [--only id,id]

Needs the shared fixture lease (``~/oa/scratch/fixture-lease``) and the
fixture state ``OPENEMR_FIXTURE_STATE`` with ``OPENEMR_UI_PORT`` /
``OPENEMR_API_PORT``. No model calls.

Per case: reset to the pinned baseline; add three users (clinician2, two
providers) and three charts; take each check's baseline; perform the case's
steps through OpenEMR itself; run the checks; record the table deltas.

- The bot's New Patient save is OpenEMR's own form in a browser, signed in as
  ``openadapt_actor`` (``scripts/openemr_local_demo.py``'s sign-in).
- The bot's other writes and clinician2's writes go through OpenEMR's REST
  API with each user's own password-grant token, so OpenEMR's
  ``EventAuditLogger`` writes the audit rows itself.
- ``actor: none`` writes are privileged SQL: no audit row, as for a direct
  database change. ``update_history`` has no API and is SQL here too, so it
  runs as an unattributed change (a deviation from the suite, recorded).

Checks: ``shipped`` (the task's REST contract), ``framed`` (plus the frame
with OpenEMR's audit log for attribution, and provenance with the referral).
The synthesized check needs ``--db`` (SQLite) and is measured on the stand-in.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Optional

import yaml

HERE = Path(__file__).resolve().parent
BENCH = HERE.parents[1]
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(BENCH.parent / "scripts"))

from scoreboard.claims_openemr import TOKEN_ENV  # noqa: E402
from scoreboard.tasks import openemr  # noqa: E402

from verification_coverage.independent.run import (  # noqa: E402
    BOT,
    CONCURRENT_PERSON,
    LABELS,
    OTHER,
    REFERRAL,
    SEEDS,
    SHIPPED_PARAMS,
    SHIPPED_RECORD,
    provenance_for,
    referral_text,
    score,
)

AUDIT_USER = "oa_audit_reader"
AUDIT_ENV = "VC_AUDIT_PASSWORD"
AUDIT_QUERY = (
    "SELECT id, user, patient_id FROM log WHERE event LIKE 'patient-record-%' "
    "AND event <> 'patient-record-select' AND patient_id > 0"
)
#: The actor client's scope, widened for the bot's stray writes (synthetic
#: fixture only; restored by every reset).
WIDE_SCOPE = (
    "openid api:oemr user/patient.crus user/allergy.crus user/message.crus "
    "user/insurance.crus user/insurance_company.crus"
)


def fx() -> Any:
    return openemr.fixture()


def sql(statement: str) -> str:
    return fx()._db(statement).decode()


def q(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


# -- setup -----------------------------------------------------------------------


def add_user(username: str, fname: str, lname: str) -> int:
    """A copy of the actor's user (same password and ACL group), renamed."""

    sql(
        "CREATE TEMPORARY TABLE openemr.iv_u AS SELECT * FROM openemr.users "
        f"WHERE username = {q(BOT)};"
        "UPDATE openemr.iv_u SET id = (SELECT MAX(id) + 1 FROM openemr.users), "
        f"uuid = UNHEX(REPLACE(UUID(), '-', '')), username = {q(username)}, "
        f"fname = {q(fname)}, lname = {q(lname)}, authorized = 1;"
        "INSERT INTO openemr.users SELECT * FROM openemr.iv_u;"
        "CREATE TEMPORARY TABLE openemr.iv_s AS SELECT * FROM openemr.users_secure "
        f"WHERE username = {q(BOT)};"
        f"UPDATE openemr.iv_s SET id = (SELECT id FROM openemr.users WHERE username = {q(username)}), "
        f"username = {q(username)};"
        "INSERT INTO openemr.users_secure SELECT * FROM openemr.iv_s;"
        "SET @aro = (SELECT MAX(id) + 1 FROM openemr.gacl_aro);"
        "INSERT INTO openemr.gacl_aro (id, section_value, value, order_value, name, hidden) "
        f"VALUES (@aro, 'users', {q(username)}, 10, {q(fname + ' ' + lname)}, 0);"
        "UPDATE openemr.gacl_aro_seq SET id = @aro;"
        "INSERT INTO openemr.gacl_groups_aro_map (group_id, aro_id) SELECT m.group_id, @aro "
        "FROM openemr.gacl_groups_aro_map m JOIN openemr.gacl_aro a ON a.id = m.aro_id "
        f"WHERE a.value = {q(BOT)};"
    )
    return int(
        sql(f"SELECT id FROM openemr.users WHERE username = {q(username)}").split()[0]
    )


def token(username: str) -> Any:
    """A password-grant API session for ``username`` (the actor's client)."""

    import httpx

    f = fx()
    clients = json.loads(f.clients_path.read_text())
    values = f._runtime_values()
    session = httpx.Client(verify=False, timeout=30)
    resp = session.post(
        f"{f.api_base_url}/oauth2/default/token",
        data={
            "grant_type": "password",
            "client_id": clients["actor_client_id"],
            "scope": WIDE_SCOPE,
            "user_role": "users",
            "username": username,
            "password": values["OPENEMR_ACTOR_PASSWORD"],
        },
    )
    if resp.status_code != 200:
        raise SystemExit(f"token for {username}: HTTP {resp.status_code}")
    session.headers["Authorization"] = "Bearer " + resp.json()["access_token"]
    return session


def api(session: Any, method: str, path: str, body: Optional[dict] = None) -> Any:
    resp = session.request(
        method, f"{fx().api_base_url}/apis/default/api{path}", json=body
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"{method} {path}: HTTP {resp.status_code} {resp.text[:300]}"
        )
    return resp.json()


def patient(pid: int) -> dict[str, str]:
    cols = ("pid", "uuid", "fname", "lname", "email")
    out = sql(
        "SELECT pid, LOWER(INSERT(INSERT(INSERT(INSERT(HEX(uuid),9,0,'-'),14,0,'-'),"
        "19,0,'-'),24,0,'-')), fname, lname, email FROM openemr.patient_data "
        f"WHERE pid = {int(pid)}"
    ).split("\t")
    return dict(zip(cols, (v.strip() for v in out)))


def seed() -> dict[str, Any]:
    f = fx()
    clients = json.loads(f.clients_path.read_text())
    sql(
        f"UPDATE openemr.oauth_clients SET scope = {q(WIDE_SCOPE)} "
        f"WHERE client_id = {q(clients['actor_client_id'])};"
    )
    ids: dict[str, Any] = {
        OTHER: add_user(OTHER, "Casey", "Clinician"),
        "iholt": add_user("iholt", "Iris", "Holt"),
        "ohale": add_user("ohale", "Owen", "Hale"),
    }
    bot = token(BOT)
    try:
        for key, values in SEEDS.items():
            api(bot, "POST", "/patient", values)
            pid = sql(
                f"SELECT pid FROM openemr.patient_data WHERE email = {q(values['email'])}"
            ).split()[0]
            ids[key] = int(pid)
            # The API path writes no history_data; the UI does. Give each
            # seeded chart the row the UI would have, so F2 has a target.
            sql(
                f"INSERT INTO openemr.history_data (pid, date) VALUES ({int(pid)}, NOW())"
            )
    finally:
        bot.close()
    return ids


# -- the bot's New Patient save, through OpenEMR's own form ------------------------


class Browser:
    def __init__(self) -> None:
        from openemr_local_demo import (
            OpenEMRPlaywrightBackend,
            _preauthenticate_browser,
        )

        self.backend, self._close = OpenEMRPlaywrightBackend.launch(
            fx().ui_base_url, headless=True
        )
        _preauthenticate_browser(self.backend, fx())

    def close(self) -> None:
        self._close()

    def register(self, v: dict[str, str]) -> None:
        from openemr_local_demo import (
            FORM_FRAME_NAME,
            FORM_PATH,
            _confirm_create_locator,
            _form_frame,
        )

        page = self.backend.page
        page.evaluate(
            "({path, name}) => { window.navigateTab(path, name); "
            "window.activateTabByName(name, true); }",
            {"path": FORM_PATH, "name": FORM_FRAME_NAME},
        )
        form = _form_frame(page)
        form.locator("#form_fname").wait_for(state="visible", timeout=60_000)
        form.locator("#form_title").select_option(label=v["title"])
        form.locator("#form_fname").fill(v["fname"])
        form.locator("#form_lname").fill(v["lname"])
        form.locator("#form_DOB").fill(v["DOB"])
        form.locator("#form_sex").select_option(label=v["sex"])
        provider = form.locator("#form_providerID")
        if provider.count():
            last, first = v["provider"].removeprefix("Dr. ").split(" ")[::-1]
            provider.select_option(label=f"{last}, {first}")
        form.get_by_text("Contact", exact=True).click()
        form.locator("#form_street").fill(v["street"])
        form.locator("#form_city").fill(v["city"])
        form.locator("#form_state").select_option(label=v["state_label"])
        form.locator("#form_postal_code").fill(v["postal_code"])
        form.locator("#form_phone_home").fill(v["phone_home"])
        form.locator("#form_email").fill(v["email"])
        form.locator("#form_country_code").select_option(label=v["country_code"])
        form.locator("#create").click()
        _confirm_create_locator(page).click()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            n = sql(
                "SELECT COUNT(*) FROM openemr.patient_data "
                f"WHERE email = {q(v['email'])} AND lname = {q(v['lname'])}"
            ).split()[0]
            if int(n) >= 1:
                time.sleep(3)  # let the post-save page's writes settle
                return
            time.sleep(0.5)
        raise RuntimeError("the New Patient save did not land")


# -- one case --------------------------------------------------------------------


def api_body(values: dict[str, Any]) -> dict[str, Any]:
    keys = (
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
    body = {k: values[k] for k in keys if k in values}
    body["state"] = "MA"
    return body


def apply_steps(
    case: dict[str, Any],
    ids: dict[str, Any],
    told: dict[str, str],
    browser: Browser,
    deviations: list[str],
) -> None:
    sessions = {"bot": token(BOT), "clinician2": token(OTHER)}
    new_pid: Optional[int] = None
    try:
        for step in case["steps"]:
            who, op = step["actor"], step["op"]
            if op == "register_referral":
                if step.get("phase") == "finish":
                    continue
                browser.register(told)
                new_pid = int(
                    sql(
                        "SELECT MAX(pid) FROM openemr.patient_data "
                        f"WHERE email = {q(told['email'])}"
                    ).split()[0]
                )
                continue
            if op == "register_patient":
                api(sessions[who], "POST", "/patient", CONCURRENT_PERSON)
                continue
            pid = (
                new_pid if step.get("target") == "new_patient" else ids[step["target"]]
            )
            p = patient(pid)
            referral = api_body({**REFERRAL})
            if op in ("overwrite_chart", "update_chart"):
                if op == "overwrite_chart":
                    changes = referral
                else:
                    changes = {
                        k: (referral[k] if v == "referral" else v)
                        for k, v in step["set"].items()
                    }
                if who == "none":
                    sets = ", ".join(f"{k} = {q(v)}" for k, v in changes.items())
                    sql(
                        f"UPDATE openemr.patient_data SET {sets} WHERE pid = {int(pid)}"
                    )
                else:
                    current = api(sessions[who], "GET", f"/patient/{p['uuid']}")["data"]
                    body = {k: current.get(k) for k in api_body(current)}
                    body.update(changes)
                    api(sessions[who], "PUT", f"/patient/{p['uuid']}", body)
            elif op == "update_history":
                deviations.append("update_history ran as SQL (no API): unattributed")
                sets = ", ".join(f"{k} = {q(v)}" for k, v in step["set"].items())
                sql(f"UPDATE openemr.history_data SET {sets} WHERE pid = {int(pid)}")
            elif op == "delete_history":
                sql(f"DELETE FROM openemr.history_data WHERE pid = {int(pid)}")
            elif op == "add_note":
                api(
                    sessions[who],
                    "POST",
                    f"/patient/{pid}/message",
                    {
                        "body": step["body"],
                        "groupname": "Default",
                        "from": BOT,
                        "to": BOT,
                        "title": "Other",
                        "message_status": "New",
                    },
                )
            elif op == "add_allergy":
                api(
                    sessions[who],
                    "POST",
                    f"/patient/{p['uuid']}/allergy",
                    {
                        "title": step["title"],
                        "begdate": "2026-10-24",
                    },
                )
            elif op == "add_insurance":
                deviations.append(
                    "add_insurance ran as SQL with an audit row by the bot"
                )
                sql(
                    "INSERT INTO openemr.insurance_data (type, provider, pid, date) VALUES "
                    f"('primary', '0', {int(pid)}, CURDATE());"
                    "INSERT INTO openemr.log (date, event, category, user, groupname, "
                    "comments, patient_id, success, log_from) VALUES (NOW(), "
                    f"'patient-record-insert', 'Insurance', {q(BOT)}, 'Default', '', "
                    f"{int(pid)}, 1, 'open-emr')"
                )
            else:
                raise ValueError(op)
    finally:
        for s in sessions.values():
            s.close()


def run_case(case: dict[str, Any], out: Path, container: str) -> dict[str, Any]:
    from openadapt_flow.provenance import (
        cross_check,
        default_extractor,
        document_sha256,
        read_sources,
    )
    from openadapt_flow.verify_run import (
        capture_baseline,
        parse_verify_config,
        verify_run,
    )

    f = fx()
    cdir = out / case["id"]
    cdir.mkdir(parents=True, exist_ok=True)
    f.reset()
    ids = seed()
    told = {**REFERRAL, **case.get("told", {})}
    doc = cdir / "referral.txt"
    doc.write_text(referral_text(REFERRAL))
    documents = read_sources([doc])
    doc_sha = document_sha256(doc)
    params = {k: told[k] for k in SHIPPED_PARAMS}
    oracle = f.token_session("oracle")
    os.environ[TOKEN_ENV] = oracle.headers["Authorization"].removeprefix("Bearer ")
    oracle.close()
    base = {
        "schema_version": "openadapt.verify/v1",
        "verifier": {
            "kind": "rest",
            "base_url": f.ui_base_url,
            "records_path": "/apis/default/api/patient?_limit=1000",
            "records_key": "data",
            "auth": {"bearer_env": TOKEN_ENV},
        },
        "record": SHIPPED_RECORD,
    }
    framed = {
        **base,
        "frame": {
            "identity_field": "pid",
            "attribution": {
                "kind": "audit_read",
                "actor_field": "user",
                "record_field": "patient_id",
                "self": [BOT],
                "read": {
                    "kind": "sql",
                    "sql_driver": "verification_coverage.exec_mariadb",
                    "sql_connect_args": {
                        "container": container,
                        "user": AUDIT_USER,
                        "database": "openemr",
                    },
                    "sql_password_env": AUDIT_ENV,
                    "sql_query": AUDIT_QUERY,
                },
            },
        },
    }
    checks = {
        "shipped": (parse_verify_config(base), None),
        "framed": (
            parse_verify_config(framed),
            provenance_for(doc_sha, told, list(SHIPPED_PARAMS)),
        ),
    }
    # Signed in before the run starts, as the task's unmeasured setup is.
    browser = Browser()
    counts_before = f.table_counts()
    for name, (cfg, _p) in checks.items():
        capture_baseline(cfg, params, cdir / f"baseline-{name}.json")
    prov = provenance_for(doc_sha, told, [n for n, _ in LABELS])
    pre = cross_check(prov, documents, default_extractor(prov))
    deviations: list[str] = []
    error = None
    try:
        apply_steps(case, ids, told, browser, deviations)
    except Exception as exc:  # noqa: BLE001 - recorded, the case is void
        error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        browser.close()
    time.sleep(2)
    counts_after = f.table_counts()
    row: dict[str, Any] = {
        "id": case["id"],
        "class": case["class"],
        "expect": case["expect"],
        "presave": pre.status,
        "presave_held_fields": pre.disagreements,
        "deviations": deviations,
        "error": error,
        "table_deltas": {
            t: counts_after[t] - counts_before.get(t, 0)
            for t in counts_after
            if counts_after[t] != counts_before.get(t, 0)
        },
    }
    for name, (cfg, provenance) in checks.items():
        result = verify_run(
            cfg,
            params=params,
            run_record={"executor_kind": "agent", "executor_id": BOT},
            authorization_ref="independent-suite-live",
            baseline_path=cdir / f"baseline-{name}.json",
            provenance=provenance,
            documents=documents if provenance is not None else None,
        )
        receipt = result["receipt"]
        row[name] = {
            "outcome": receipt.get("outcome"),
            "checked": receipt.get("checked"),
            "verified": receipt.get("outcome") == "verified",
        }
        row[name]["score"] = score(case["expect"], row[name]["verified"])
        (cdir / f"evidence-{name}.json").write_text(
            json.dumps(result.get("evidence"), indent=2, sort_keys=True, default=str)
        )
    row["presave_score"] = score(case["expect"], pre.status == "agree")
    audit = sql(
        "SELECT user, event, patient_id FROM openemr.log WHERE event LIKE 'patient-record-%' "
        "AND event <> 'patient-record-select' ORDER BY id"
    )
    (cdir / "audit.tsv").write_text(audit)
    return row


def main() -> int:
    from verification_coverage.openemr_run import ensure_audit_reader

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    suite = yaml.safe_load((HERE / "faults.yaml").read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(24)
    os.environ[AUDIT_ENV] = password
    # A MariaDB account, not an OpenEMR row: it survives every reset.
    container = ensure_audit_reader(password)
    rows_path = args.out / "rows.jsonl"
    for case in suite["cases"]:
        if args.only and case["id"] not in args.only.split(","):
            continue
        try:
            row = run_case(case, args.out, container)
        except Exception as exc:  # noqa: BLE001 - recorded, the case is void
            row = {"id": case["id"], "error": f"{type(exc).__name__}: {exc}"[:500]}
        with rows_path.open("a") as fh:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        print(
            case["id"],
            {
                k: row[k].get("score")
                for k in ("shipped", "framed")
                if isinstance(row.get(k), dict)
            },
            row.get("error"),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
