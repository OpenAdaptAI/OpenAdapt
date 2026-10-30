"""EspoCRM (first study, instance 2): demonstrations, then the generator.

    FCG_INSTANCE=2 python benchmark/generate_contracts/demo_espocrm.py WORKDIR

One demonstration write per example in ``setup_view.yaml``, through
EspoCRM's REST API as the API user ``crm-bot``. Record ids for names are
looked up through the read-only account (exact, byte for byte: the name
columns use a case- and accent-insensitive collation).
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pymysql
import requests
from demo_lib import demo, fixture, propose, setup_view, write_inputs

APP = "espocrm"


def main(work: Path, extra: list[str]) -> None:
    fx = fixture(APP)
    fx.reset()
    bot = fx.bot()
    ro = fx.read_access()
    read = {
        "sql_driver": "pymysql",
        "sql_connect_args": {"host": ro["host"], "port": ro["port"], "user": ro["user"],
                             "database": ro["database"], "charset": "utf8mb4"},
        "sql_password_env": "ESPOCRM_DB_RO_PASSWORD",
    }  # fmt: skip
    write_inputs(work, APP, read)
    db = pymysql.connect(host=ro["host"], port=ro["port"], user=ro["user"], password=ro["password"],
                         database=ro["database"], charset="utf8mb4", autocommit=True)  # fmt: skip

    def one(sql: str, args: tuple = ()) -> str:
        with db.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        if len(rows) != 1:
            raise RuntimeError(f"{sql} {args}: {len(rows)} rows")
        return str(rows[0][0])

    def account(name: str, city: str) -> str:
        return one(
            "SELECT id FROM account WHERE BINARY name=%s AND BINARY billing_address_city=%s AND deleted=0",
            (name, city),
        )

    def person(table: str, full: str, company: str | None = None) -> str:
        first, last = full.split(" ", 1)
        sql = f"SELECT id FROM {table} WHERE BINARY first_name=%s AND BINARY last_name=%s AND deleted=0"
        args: tuple = (first, last)
        if company is not None:
            sql += " AND BINARY account_name=%s"
            args += (company,)
        return one(sql, args)

    def api(method: str, path: str, payload: dict) -> None:
        r = requests.request(
            method,
            bot["base_url"] + path,
            headers=bot["headers"],
            json=payload,
            timeout=60,
        )
        r.raise_for_status()

    bot_id = one("SELECT id FROM user WHERE user_name=%s", ("crm-bot",))
    for k in setup_view(APP)["kinds"]:
        kind = k["kind"]
        for ex in k["examples"]:
            v = ex["values"]
            if kind == "create_contact":
                with db.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM contact WHERE BINARY first_name=%s AND BINARY last_name=%s AND deleted=0",
                                (v["first_name"], v["last_name"]))  # fmt: skip
                    if cur.fetchone()[0]:
                        continue
                body = {"firstName": v["first_name"], "lastName": v["last_name"],
                        "accountId": account(v["account"], v["account_city"])}  # fmt: skip
                if v.get("title"):
                    body["title"] = v["title"]
                if v.get("email"):
                    body["emailAddress"] = v["email"]
                if v.get("phone"):
                    body["phoneNumber"] = v["phone"]
                demo(work, kind, v, lambda b=body: api("POST", "/Contact", b))
            elif kind == "create_opportunity":
                with db.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM opportunity WHERE BINARY name=%s AND deleted=0",
                        (v["name"],),
                    )
                    if cur.fetchone()[0]:
                        continue
                body = {"name": v["name"], "accountId": account(v["account"], v["account_city"]),
                        "amount": v["amount"], "amountCurrency": v["currency"], "stage": v["stage"],
                        "closeDate": v["close_date"]}  # fmt: skip
                demo(work, kind, v, lambda b=body: api("POST", "/Opportunity", b))
            elif kind == "update_lead":
                lid = one("SELECT id FROM lead WHERE BINARY first_name=%s AND BINARY last_name=%s "
                          "AND BINARY account_name=%s AND deleted=0",
                          (v["first_name"], v["last_name"], v["company"]))  # fmt: skip
                if v["field"] == "email":
                    body = {"emailAddressData": [{"emailAddress": v["value"], "primary": True,
                                                  "optOut": False, "invalid": False}]}  # fmt: skip
                else:
                    body = {v["field"]: v["value"]}
                demo(work, kind, v, lambda i=lid, b=body: api("PUT", f"/Lead/{i}", b))
            elif kind == "log_call":
                start = dt.datetime.strptime(v["date_start"], "%Y-%m-%d %H:%M")
                end = start + dt.timedelta(minutes=int(v["duration_minutes"]))
                body = {"name": v["subject"], "status": v["status"], "direction": v["direction"],
                        "dateStart": start.strftime("%Y-%m-%d %H:%M:%S"),
                        "dateEnd": end.strftime("%Y-%m-%d %H:%M:%S"), "assignedUserId": bot_id}  # fmt: skip
                if v.get("account"):
                    body.update(
                        parentType="Account",
                        parentId=account(v["account"], v["account_city"]),
                    )
                if v.get("lead"):
                    lead = person("lead", v["lead"], v.get("lead_company"))
                    body.update(parentType="Lead", parentId=lead, leadsIds=[lead])
                if v.get("contact"):
                    body["contactsIds"] = [person("contact", v["contact"])]
                if v.get("description"):
                    body["description"] = v["description"]
                demo(work, kind, v, lambda b=body: api("POST", "/Call", b))
    report = propose(work, {"bot_user_id": bot_id}, extra)
    print("questions:", len(report["questions"]))
    db.close()
    fx.reset()


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2:])
