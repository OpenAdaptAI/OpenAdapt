"""Redmine 6.0 (issue tracking, Rails, SQLite): open an issue in one project.

Runs from the official image as an external service; nothing from Redmine
(GPL-2.0) is copied here. Synthetic project and text.
"""

from __future__ import annotations

import json
from typing import Any

from benchmark.write_leaderboard_v2.apps.base import App, podman
from benchmark.write_leaderboard_v2.proxy import Save

IMAGE = "docker.io/library/redmine:6.0-alpine"
DB = "/usr/src/redmine/sqlite/redmine.db"
ENV = ["-u", "redmine", "-e", "RAILS_ENV=production", "-e", "REDMINE_LANG=en",
       "-e", "SECRET_KEY_BASE=synthetic-test-only-0123456789abcdef"]
PASSWORD = "synthetic-admin-pass"  # a throwaway local container's admin, not a credential
SETUP = f"""
u = User.find_by_login('admin'); u.password = '{PASSWORD}'; u.password_confirmation = '{PASSWORD}'
u.must_change_passwd = false; u.save!
p = Project.find_by_identifier('helpdesk') || Project.create!(name: 'Synthetic Helpdesk', identifier: 'helpdesk', is_public: false)
p.trackers = Tracker.all; p.enabled_module_names = ['issue_tracking']; p.save!
"""
JUDGE = ("SELECT i.id AS id, i.subject AS subject, COALESCE(i.description, '') AS description, t.name AS tracker, "
         "COALESCE(i.estimated_hours, '') AS estimated_hours, p.name AS priority, s.name AS status, "
         "COALESCE(i.assigned_to_id, '') AS assigned_to_id, COALESCE(i.start_date, '') AS start_date, "
         "COALESCE(i.due_date, '') AS due_date, i.done_ratio AS done_ratio FROM issues i "
         "JOIN trackers t ON t.id = i.tracker_id JOIN enumerations p ON p.id = i.priority_id "
         "JOIN issue_statuses s ON s.id = i.status_id ORDER BY i.id")


class Redmine(App):
    name = "redmine"
    description = "Redmine, the team's issue tracker"
    record_noun = "issue"
    login_marker = "#login-submit"
    columns = {"subject": "text", "description": "text", "tracker": "text", "estimated_hours": "number",
               "priority": "text", "status": "text", "assigned_to_id": "text", "due_date": "date", "done_ratio": "text"}
    save = Save(r"^/projects/helpdesk/issues$")
    body_field = {"subject": "issue[subject]", "description": "issue[description]",
                  "estimated_hours": "issue[estimated_hours]", "due_date": "issue[due_date]"}
    home_path = "/projects/helpdesk"
    save_ok_status = frozenset({302})

    @property
    def c(self) -> str:
        return f"lbv2-redmine-{self.slot}"

    def sql(self, query: str, *, write: bool = False) -> list[dict[str, Any]]:
        ro = "false" if write else "true"
        script = (f'db=SQLite3::Database.new("{DB}", readonly: {ro}); db.busy_timeout=5000; db.results_as_hash=true; '
                  f'r=db.execute(STDIN.read); puts JSON.dump(r.map {{|h| h.reject {{|k,_| k.is_a?(Integer)}} }})')
        out = podman("exec", "-i", self.c, "ruby", "-rsqlite3", "-rjson", "-e", script, input=query)
        return json.loads(out or "[]")

    def rails(self, code: str, timeout: float = 600) -> str:
        return podman("exec", "-i", *ENV, self.c, "sh", "-c", "cat > /tmp/lb.rb && bundle exec rails runner /tmp/lb.rb",
                      input=code, timeout=timeout)

    def up(self) -> None:
        import subprocess

        if subprocess.run(["podman", "container", "exists", self.c]).returncode == 0:
            podman("start", self.c)
            self.wait_http("/login")
            return
        podman("run", "-d", "--name", self.c, "-p", f"127.0.0.1:{self.upstream}:3000",
               "-e", "REDMINE_SECRET_KEY_BASE=synthetic-test-only-0123456789abcdef", IMAGE)
        self.wait_http("/login")
        podman("exec", *ENV, self.c, "bundle", "exec", "rake", "redmine:load_default_data", timeout=600)
        self.rails(SETUP)

    def down(self) -> None:
        podman("rm", "-f", self.c, check=False)

    def login(self, page: Any) -> None:
        page.goto(self.base + "/login")
        page.fill("#username", "admin")
        page.fill("#password", PASSWORD)
        page.click("#login-submit")
        page.wait_for_load_state("load")
        if "/login" in page.url:
            raise RuntimeError("the harness couldn't sign in to Redmine")

    def signed_out(self, url: str) -> bool:
        return "/login" in url

    def rows(self) -> list[dict[str, Any]]:
        return [{k: ("" if v is None else str(v)) for k, v in r.items()} for r in self.sql(JUDGE)]

    def seed(self, issues: list[dict[str, Any]]) -> None:
        have = {r["subject"] for r in self.rows()}
        todo = [i for i in issues if i["subject"] not in have]
        if not todo:
            return
        code = "p = Project.find_by_identifier('helpdesk'); u = User.find_by_login('admin')\n"
        for i in todo:
            code += (f"Issue.create!(project: p, author: u, tracker: Tracker.find_by_name({json.dumps(i['tracker'])}), "
                     f"subject: {json.dumps(i['subject'])}, description: {json.dumps(i.get('description', ''))}, "
                     f"priority: IssuePriority.find_by_name({json.dumps(i.get('priority', 'Normal'))}))\n")
        self.rails(code)

    def reset(self, task: dict[str, Any], seed: list[dict[str, Any]]) -> None:
        keep = {i["subject"] for i in seed}
        code = "p = Project.find_by_identifier('helpdesk')\n"
        code += f"p.issues.reject {{ |i| {json.dumps(sorted(keep))}.include?(i.subject) }}.each(&:destroy)\n"
        for i in seed:
            code += (f"i = Issue.find_by_subject({json.dumps(i['subject'])}); if i then i.update_columns("
                     f"description: {json.dumps(i.get('description', ''))}, "
                     f"priority_id: IssuePriority.find_by_name({json.dumps(i.get('priority', 'Normal'))}).id, "
                     f"status_id: IssueStatus.find_by_name('New').id, assigned_to_id: nil, due_date: nil, "
                     "estimated_hours: nil, done_ratio: 0) end\n")
        self.rails(code)
        self.seed(seed)

    def side_write(self, target: dict[str, Any], change: dict[str, Any]) -> None:
        from benchmark.write_leaderboard_v2.apps.base import sql_str

        subj = sql_str(target["subject"])
        if "priority" in change:
            self.sql(f"UPDATE issues SET priority_id = (SELECT id FROM enumerations WHERE type = 'IssuePriority' "
                     f"AND name = {sql_str(change['priority'])}) WHERE subject = {subj}", write=True)
        if "status" in change:
            self.sql(f"UPDATE issues SET status_id = (SELECT id FROM issue_statuses WHERE name = "
                     f"{sql_str(change['status'])}) WHERE subject = {subj}", write=True)

    def expect(self, t: dict[str, Any]) -> dict[str, str]:
        return {"subject": t["subject"], "description": t["description"], "tracker": t["tracker"],
                "estimated_hours": t["estimated_hours"], "priority": t["priority"], "due_date": t["due_date"],
                "status": "New", "assigned_to_id": ""}

    def task_text(self, t: dict[str, Any]) -> str:
        return (f"In the Synthetic Helpdesk project, open a new issue: tracker {t['tracker']}, "
                f"subject \"{t['subject']}\", description \"{t['description']}\", priority {t['priority']}, "
                f"due date {t['due_date']}, estimated time {t['estimated_hours']} hours. "
                "Fill in exactly these values and leave every other field as it is.")

    def scripted(self, page: Any, t: dict[str, Any]) -> str:
        """Model-free harness check only: the form filled by selectors."""

        page.goto(self.base + "/projects/helpdesk/issues/new")
        page.select_option("#issue_tracker_id", label=t["tracker"])
        page.wait_for_timeout(1500)
        page.fill("#issue_subject", t["subject"])
        page.fill("#issue_description", t["description"])
        page.select_option("#issue_priority_id", label=t["priority"])
        page.fill("#issue_due_date", t["due_date"])
        page.fill("#issue_estimated_hours", t["estimated_hours"])
        page.click("input[name=commit]")
        page.wait_for_load_state("load")
        return f"RESULT: DONE {page.url}"
