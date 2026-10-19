"""Redmine 6.0 (ticketing, Ruby on Rails, SQLite): a new issue in one project.

Runs as an external service from the official image; nothing from Redmine
(GPL-2.0) is copied here. Synthetic project, synthetic text.
"""

from __future__ import annotations

import json
from typing import Any

from benchmark.api_generalization.common import (
    AppBase,
    ExecSqlVerifier,
    podman,
    rec_click,
    rec_select,
    rec_text,
)
from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr

C = "apigen-redmine"
IMAGE = "docker.io/library/redmine:6.0-alpine"
DB = "/usr/src/redmine/sqlite/redmine.db"
ENV = ["-u", "redmine", "-e", "RAILS_ENV=production", "-e", "SECRET_KEY_BASE=synthetic-test-only-0123456789abcdef",
       "-e", "REDMINE_LANG=en"]
PASSWORD = "synthetic-admin-pass"  # a throwaway local container's admin, not a real credential
SEED = f"""
u = User.find_by_login('admin'); u.password = '{PASSWORD}'; u.password_confirmation = '{PASSWORD}'
u.must_change_passwd = false; u.save!
p = Project.find_by_identifier('helpdesk') || Project.create!(name: 'Synthetic Helpdesk', identifier: 'helpdesk', is_public: false)
p.trackers = Tracker.all; p.enabled_module_names = ['issue_tracking']; p.save!
"""
QUERY = ("SELECT i.id, i.subject, COALESCE(i.description, '') AS description, t.name AS tracker, "
         "COALESCE(i.estimated_hours, '') AS estimated_hours, p.name AS priority "
         "FROM issues i JOIN trackers t ON t.id = i.tracker_id JOIN enumerations p ON p.id = i.priority_id "
         "WHERE i.project_id = 1")
JUDGE = ("SELECT i.*, t.name AS tracker, p.name AS priority, s.name AS status FROM issues i "
         "JOIN trackers t ON t.id = i.tracker_id JOIN enumerations p ON p.id = i.priority_id "
         "JOIN issue_statuses s ON s.id = i.status_id ORDER BY i.id")


def sql(query: str, *, write: bool = False) -> list[dict[str, Any]]:
    ro = "false" if write else "true"
    script = (f'db=SQLite3::Database.new("{DB}", readonly: {ro}); db.busy_timeout=5000; db.results_as_hash=true; '
              f'r=db.execute(STDIN.read); puts JSON.dump(r.map {{|h| h.reject {{|k,_| k.is_a?(Integer)}} }})')
    out = podman("exec", "-i", C, "ruby", "-rsqlite3", "-rjson", "-e", script, input=query)
    return json.loads(out or "[]")


class App(AppBase):
    name = "redmine"
    workflow = "new-issue"
    upstream_port = 9821
    proxy_port = 9822
    form_path = "/projects/helpdesk/issues/new"
    save_path_regex = r"^/projects/helpdesk/issues$"
    unique_param = "subject"
    normal_values = [
        dict(tracker="Bug", subject="Badge printer jams on second floor", description="Paper feed stalls after ten sheets.", estimated_hours="1.5"),
        dict(tracker="Support", subject="Reset shared calendar permissions", description="Front desk cannot see room bookings.", estimated_hours="0.25"),
        dict(tracker="Feature", subject="Add export of visitor log", description="Weekly CSV for the facilities team.", estimated_hours="3.5"),
    ]
    blank_values = dict(tracker="Bug", subject="Kiosk screen flickers", description="", estimated_hours="")
    dup_values = dict(tracker="Support", subject="Same words twice", description="Same words twice", estimated_hours="2.5")
    invalid_values = dict(tracker="Bug", subject="Hours field holds text", description="Validation case.", estimated_hours="abc")
    invalid_note = "estimated hours 'abc' (the server rejects it)"
    default_column = "priority"
    changed_default_value = "High"

    def up(self) -> None:
        podman("run", "-d", "--name", C, "-p", f"127.0.0.1:{self.upstream_port}:3000",
               "-e", "REDMINE_SECRET_KEY_BASE=synthetic-test-only-0123456789abcdef", IMAGE)
        self.wait_http("/login")
        podman("exec", *ENV, C, "bundle", "exec", "rake", "redmine:load_default_data", timeout=600)
        podman("exec", "-i", *ENV, C, "sh", "-c", "cat > /tmp/seed.rb && bundle exec rails runner /tmp/seed.rb",
               input=SEED, timeout=600)

    def down(self) -> None:
        podman("rm", "-f", C, check=False)

    def login(self, page: Any) -> None:
        page.goto(self.base + "/login")
        page.fill("#username", "admin")
        page.fill("#password", PASSWORD)
        page.click("#login-submit")
        page.wait_for_load_state("load")

    def record(self, rec: Any, page: Any, v: dict[str, str]) -> None:
        rec_select(rec, page, "#issue_tracker_id", v["tracker"], "tracker")
        page.wait_for_timeout(1500)  # the tracker change re-renders the form's attributes
        rec_text(rec, page, "#issue_subject", v["subject"], "subject")
        rec_text(rec, page, "#issue_description", v["description"], "description")
        rec_text(rec, page, "#issue_estimated_hours", v["estimated_hours"], "estimated_hours")
        rec_click(rec, page, "input[name=commit]")

    def expire_session(self, page: Any) -> None:
        sql("DELETE FROM tokens WHERE action = 'session'", write=True)

    def change_default(self) -> None:
        sql("UPDATE enumerations SET is_default = (name = 'High') WHERE type = 'IssuePriority'", write=True)

    def restore_default(self) -> None:
        sql("UPDATE enumerations SET is_default = (name = 'Normal') WHERE type = 'IssuePriority'", write=True)

    def rows(self) -> list[dict[str, Any]]:
        return [r | {"estimated_hours": "" if r.get("estimated_hours") is None else str(r["estimated_hours"])}
                for r in sql(JUDGE)]

    def verifier(self) -> Any:
        return ExecSqlVerifier(sql, QUERY, timeout_s=10.0)

    def expect_fields(self, v: dict[str, str]) -> dict[str, str]:
        return {"subject": v["subject"], "description": v["description"], "tracker": v["tracker"],
                "estimated_hours": v["estimated_hours"], "priority": "Normal"}

    def effects(self) -> list[Effect]:
        match = {"subject": ValueExpr(param="subject")}
        out = [Effect(kind=EffectKind.RECORD_WRITTEN, match=match, expected_count=1, risk="reversible",
                      probe="exactly one issue with this subject", timeout_s=8.0)]
        for f in ("tracker", "description", "estimated_hours"):
            out.append(Effect(kind=EffectKind.FIELD_EQUALS, match=match, field=f, value=ValueExpr(param=f),
                              risk="reversible", probe=f"issue {f} read-back", timeout_s=8.0))
        return out
