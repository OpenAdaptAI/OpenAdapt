"""Redmine (first study, instance 1): demonstrations, then the generator.

    FCG_INSTANCE=1 python benchmark/generate_contracts/demo_redmine.py WORKDIR

One demonstration write per example in ``setup_view.yaml``, through
Redmine's REST API as the bot. Ids for names (project, tracker, user) are
looked up through the read-only account, as a setup agent would.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg2
import requests
from demo_lib import demo, fixture, propose, setup_view, write_inputs

APP = "redmine"


def main(work: Path) -> None:
    fx = fixture(APP)
    fx.reset()
    bot = fx.bot()
    ro = fx.read_access()
    read = {
        "sql_driver": "psycopg2",
        "sql_connect_args": {
            "host": ro["host"],
            "port": ro["port"],
            "user": ro["user"],
            "dbname": ro["database"],
        },
        "sql_password_env": "REDMINE_RO_DB_PASSWORD",
    }
    write_inputs(work, APP, read)
    db = psycopg2.connect(ro["dsn"])
    db.autocommit = True

    def one(sql: str, args: tuple = ()) -> object:
        with db.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        if len(rows) != 1:
            raise RuntimeError(f"{sql} {args}: {len(rows)} rows")
        return rows[0][0]

    def project(name: str) -> int:
        return int(one("SELECT id FROM projects WHERE name=%s", (name,)))

    def user(name: str) -> int:
        return int(
            one("SELECT id FROM users WHERE firstname || ' ' || lastname = %s", (name,))
        )

    def enum(kind: str, name: str) -> int:
        return int(
            one("SELECT id FROM enumerations WHERE type=%s AND name=%s", (kind, name))
        )

    def issue(proj: str, subject: str) -> int:
        return int(one("SELECT i.id FROM issues i JOIN projects p ON p.id=i.project_id "
                       "WHERE p.name=%s AND i.subject=%s", (proj, subject)))  # fmt: skip

    def field(name: str) -> int:
        return int(one("SELECT id FROM custom_fields WHERE name=%s", (name,)))

    def api(method: str, path: str, payload: object) -> None:
        r = requests.request(
            method,
            bot["base_url"] + path,
            headers=bot["headers"],
            json=payload,
            timeout=60,
        )
        r.raise_for_status()

    def create_payload(v: dict) -> dict:
        body: dict = {
            "project_id": project(v["project"]),
            "tracker_id": int(
                one("SELECT id FROM trackers WHERE name=%s", (v["tracker"],))
            ),
            "subject": v["subject"],
            "priority_id": enum("IssuePriority", v["priority"]),
        }
        if v.get("assignee"):
            body["assigned_to_id"] = user(v["assignee"])
        if v.get("due_date"):
            body["due_date"] = v["due_date"]
        if v.get("description"):
            body["description"] = v["description"]
        cf = [
            {"id": field(n), "value": v[k]}
            for k, n in (
                ("customer_ref", "Customer Ref"),
                ("environment", "Environment"),
            )
            if v.get(k) is not None
        ]
        if cf:
            body["custom_fields"] = cf
        return {"issue": body}

    for k in setup_view(APP)["kinds"]:
        kind = k["kind"]
        for ex in k["examples"]:
            v = ex["values"]
            if kind in ("create_issue", "ensure_issue"):
                if kind == "ensure_issue" and int(one(
                        "SELECT COUNT(*) FROM issues i JOIN projects p ON p.id=i.project_id "
                        "WHERE p.name=%s AND i.subject=%s", (v["project"], v["subject"]))):  # fmt: skip
                    continue
                demo(
                    work,
                    kind,
                    v,
                    lambda p=create_payload(v): api("POST", "/issues.json", p),
                )
            elif kind == "update_issue":
                body: dict = {}
                if v.get("status"):
                    body["status_id"] = int(
                        one(
                            "SELECT id FROM issue_statuses WHERE name=%s",
                            (v["status"],),
                        )
                    )
                if v.get("assignee"):
                    body["assigned_to_id"] = user(v["assignee"])
                if v.get("priority"):
                    body["priority_id"] = enum("IssuePriority", v["priority"])
                for key in ("due_date", "done_ratio", "notes"):
                    if v.get(key) is not None:
                        body[key] = v[key]
                iid = issue(v["project"], v["subject"])
                demo(
                    work,
                    kind,
                    v,
                    lambda i=iid, b=body: api("PUT", f"/issues/{i}.json", {"issue": b}),
                )
            elif kind == "log_time":
                body = {"issue_id": issue(v["project"], v["issue_subject"]), "hours": v["hours"],
                        "spent_on": v["spent_on"], "activity_id": enum("TimeEntryActivity", v["activity"]),
                        "comments": v["comments"]}  # fmt: skip
                demo(
                    work,
                    kind,
                    v,
                    lambda b=body: api("POST", "/time_entries.json", {"time_entry": b}),
                )
    bot_id = one("SELECT id FROM users WHERE login=%s", (bot["login"],))
    report = propose(work, {"bot_user_id": str(bot_id)})
    print("questions:", len(report["questions"]))
    db.close()
    fx.reset()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
