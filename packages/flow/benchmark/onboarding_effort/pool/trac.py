"""Trac 1.6 workflows (BSD-3-Clause service desk, synthetic tickets).

The engineer's verify.yaml reads Trac's SQLite database through the live file,
bind-mounted from the host (``kind: sql``, ``sqlite_database``). The oracle reads
the live database inside the container through ``podman exec``.
"""

from __future__ import annotations

import os
import random
from typing import Any

from benchmark.onboarding_effort.apps import trac_app as T
from benchmark.onboarding_effort.pool.base import (
    FIRST,
    Verdict,
    Workflow,
    choose,
    click,
    fill,
)

COMPONENTS = ("intake", "billing", "network", "facilities")
MILESTONES = ("2026-Q4", "2027-Q1", "2027-Q2")
WORDS = (
    "printer", "badge reader", "laptop dock", "monitor", "VPN token", "desk phone",
    "projector", "label maker", "scanner", "headset", "router", "keyboard",
)
PROBLEMS = (
    "does not power on", "drops the connection", "shows an error on start",
    "needs a replacement part", "is slow after the update", "was left in room 4",
)
TYPES = ("defect", "task", "enhancement")
PRIORITIES = ("minor", "major", "critical")


def host_db() -> str:
    """The engineer's read path: Trac's live database file, bind-mounted."""
    return os.path.join(T.host_db_dir(), "trac.db")


class TracBase(Workflow):
    app = "trac"
    policy = "permissive"
    seed = 7

    def user(self, installation: str) -> str:
        return "admin"

    def reset(self, installation: str) -> None:
        T.reset()
        self.apply_installation(installation)

    def entry_url(self, installation: str) -> str:
        return T.BASE + "/"

    def start(self, page: Any, installation: str) -> None:
        T.login(page, self.user(installation))
        page.goto(self.entry_url(installation))
        page.wait_for_load_state("load")

    def snapshot(self) -> Any:
        tickets = {r["id"]: r for r in T.tsql("SELECT * FROM ticket")}
        changes = T.tsql("SELECT ticket, time, author, field, oldvalue, newvalue FROM ticket_change")
        return {"tickets": tickets, "changes": changes}

    def _sql_verifier(self, query: str) -> str:
        return f"""verifier:
  kind: sql
  sqlite_database: {host_db()}
  sql_query: "{query}"
"""


class TracNewTicket(TracBase):
    id = "tr-ticket-new"
    shape = "single"
    inputs = ("summary", "description", "type", "priority", "component")
    intent = "File a new service-desk ticket with its summary, description, type, priority and component."
    second_installation = "a new custom field 'Site' (a select, default HQ) added to the ticket form (trac.ini [ticket-custom])"
    second_kind = "custom_field"

    def apply_installation(self, installation: str) -> None:
        if installation == FIRST:
            for opt in ("site", "site.label", "site.options", "site.value"):
                T.admin_quiet("config", "remove", "ticket-custom", opt)
        else:
            T.admin("config", "set", "ticket-custom", "site", "select")
            T.admin("config", "set", "ticket-custom", "site.label", "Site")
            T.admin("config", "set", "ticket-custom", "site.options", "|HQ|Branch North|Branch South")
            T.admin("config", "set", "ticket-custom", "site.value", "HQ")

    def entry_url(self, installation: str) -> str:
        return T.BASE + "/newticket"

    def make_item(self, rng: random.Random, n: int, split: str) -> dict[str, str]:
        thing = rng.choice(WORDS)
        return {
            "summary": f"The {thing} {rng.choice(PROBLEMS)} ({split} {n:02d}-{rng.randrange(10**5):05d})",
            "description": f"Reported at the front desk. Synthetic ticket {rng.randrange(10**6)}.",
            "type": rng.choice(TYPES),
            "priority": rng.choice(PRIORITIES),
            "component": rng.choice(COMPONENTS),
        }

    def demonstrate(self, rec: Any, page: Any, item: dict[str, str]) -> None:
        fill(page, rec, "#field-summary", item["summary"], "summary")
        fill(page, rec, "#field-description", item["description"], "description")
        choose(page, rec, "#field-type", item["type"], "type")
        choose(page, rec, "#field-priority", item["priority"], "priority")
        choose(page, rec, "#field-component", item["component"], "component")
        click(page, rec, "input[name=submit][value='Create ticket']")
        page.wait_for_url("**/ticket/*")

    def verify_yaml(self, installation: str) -> str:
        return f"""schema_version: openadapt.verify/v1
app:
  name: Trac service desk (local test copy)
  url: {self.entry_url(installation)}
  version: trac@1.6
  session: benchmark.onboarding_effort.hooks:open_session
  reset:
    python: benchmark.onboarding_effort.hooks:reset
{self._sql_verifier("SELECT id, summary, description, type, priority, component, reporter FROM ticket")}record:
  name: ticket
  match: [summary]
  duplicate_key: summary
  fields:
    summary: summary
    description: description
    type: type
    priority: priority
    component: component
save_step: auto
inputs:
  source: recorded
  unique: [summary]
campaign:
  id: onboarding-{self.id}-{installation}
  trials: 3
  policy: {self.policy}
"""

    def judge(self, item: dict[str, str], before: Any, after: Any) -> Verdict:
        new = [t for k, t in after["tickets"].items() if k not in before["tickets"]]
        mine = [t for t in new if t["summary"] == item["summary"]]
        changed = [k for k, t in before["tickets"].items() if after["tickets"].get(k) != t]
        if not mine:
            return Verdict("MISSING", {"new": len(new)})
        t = mine[0]
        want = {k: item[k] for k in ("description", "type", "priority", "component")}
        got = {k: t.get(k) for k in want}
        ok = len(mine) == 1 and len(new) == 1 and got == want and not changed
        return Verdict("VERIFIED" if ok else "WRONG", {"want": want, "got": got,
                                                       "dupes": len(mine), "changed": changed})


class TracResolve(TracBase):
    id = "tr-ticket-resolve"
    shape = "conditional"
    inputs = ("ticket", "resolution", "duplicate_of")
    intent = (
        "Find a ticket by number and close it with the given resolution; when the "
        "resolution is 'duplicate', also comment which ticket it duplicates."
    )
    second_installation = "signed in as 'triager', a non-admin role with TICKET_MODIFY only"
    second_kind = "role"
    keys = ("ticket",)

    def branch_key(self, item: dict[str, str]) -> str:
        return "duplicate" if item["resolution"] == "duplicate" else "other"

    def user(self, installation: str) -> str:
        return "admin" if installation == FIRST else "triager"

    def apply_installation(self, installation: str) -> None:
        if installation != FIRST:
            T.admin("permission", "add", "triager", "TICKET_MODIFY", "TICKET_VIEW")

    def make_item(self, rng: random.Random, n: int, split: str) -> dict[str, str]:
        res = rng.choice(("fixed", "fixed", "wontfix", "duplicate", "worksforme"))
        return {"ticket": "", "resolution": res, "duplicate_of": "",
                "_summary": f"{rng.choice(WORDS).capitalize()} {rng.choice(PROBLEMS)} [{split}-{n:02d}]"}

    def prepare_items(self, items: list[dict[str, str]], installation: str) -> None:
        anchor = _create_ticket("Original report kept open for duplicates", "intake")
        for item in items:
            item["ticket"] = str(_create_ticket(item["_summary"], "intake"))
            if item["resolution"] == "duplicate":
                item["duplicate_of"] = str(anchor)

    def demonstrate(self, rec: Any, page: Any, item: dict[str, str]) -> None:
        fill(page, rec, "#proj-search", "#" + item["ticket"], "ticket")
        rec.press("Enter")
        page.wait_for_url(f"**/ticket/{item['ticket']}")
        open_modify(page, rec)
        click(page, rec, "#action_resolve")
        choose(page, rec, "#action_resolve_resolve_resolution", item["resolution"], "resolution")
        if item["resolution"] == "duplicate":
            fill(page, rec, "#comment", "Duplicate of #" + item["duplicate_of"], "duplicate_of")
        click(page, rec, "#propertyform input[name=submit]")
        page.wait_for_selector(".trac-status:has-text('closed'), span.status:has-text('closed')")

    def verify_yaml(self, installation: str) -> str:
        return f"""schema_version: openadapt.verify/v1
app:
  name: Trac service desk (local test copy)
  url: {self.entry_url(installation)}
  version: trac@1.6
  session: benchmark.onboarding_effort.hooks:open_session
  reset:
    python: benchmark.onboarding_effort.hooks:reset
{self._sql_verifier("SELECT CAST(id AS TEXT) AS ticket, status, resolution FROM ticket")}effects:
  - kind: field_equals
    match: {{ticket: {{param: ticket}}}}
    field: resolution
    value: {{param: resolution}}
  - kind: field_equals
    match: {{ticket: {{param: ticket}}}}
    field: status
    value: {{literal: closed}}
save_step: auto
inputs:
  source: recorded
campaign:
  id: onboarding-{self.id}-{installation}
  trials: 3
  policy: {self.policy}
"""

    def judge(self, item: dict[str, str], before: Any, after: Any) -> Verdict:
        tid = int(item["ticket"])
        t0, t1 = before["tickets"].get(tid), after["tickets"].get(tid)
        if t1 is None:
            return Verdict("WRONG", {"reason": "ticket gone"})
        others = [k for k, t in before["tickets"].items() if k != tid and after["tickets"].get(k) != t]
        if t1["status"] == (t0 or {}).get("status") and not others:
            return Verdict("MISSING", {})
        comments = [c for c in after["changes"] if c["ticket"] == tid and c["field"] == "comment"
                    and c not in before["changes"]]
        text = " ".join(c["newvalue"] or "" for c in comments)
        dup_ok = (f"#{item['duplicate_of']}" in text) if item["resolution"] == "duplicate" else ("Duplicate of" not in text)
        ok = t1["status"] == "closed" and t1["resolution"] == item["resolution"] and dup_ok and not others
        return Verdict("VERIFIED" if ok else "WRONG", {"status": t1["status"], "resolution": t1["resolution"],
                                                       "comment": text[:120], "others": others})


class TracTriageLoop(TracBase):
    id = "tr-triage-loop"
    shape = "loop_search"
    batch_run = True
    inputs = ("milestone",)
    intent = (
        "Open the saved report 'Untriaged intake'; for each ticket in it, open it, set its "
        "milestone, add the comment 'Triaged.', submit, and go back to the report through "
        "View Tickets, until none are left."
    )
    second_installation = "the Trac UI in German (trac.ini [trac] default_language = de)"
    second_kind = "locale"

    def apply_installation(self, installation: str) -> None:
        T.admin("config", "set", "trac", "default_language", "" if installation == FIRST else "de")

    def entry_url(self, installation: str) -> str:
        return T.BASE + "/report/9"

    def make_item(self, rng: random.Random, n: int, split: str) -> dict[str, str]:
        return {"milestone": MILESTONES[n % len(MILESTONES)] if split.startswith("demo") else "2027-Q1",
                "_summary": f"{rng.choice(WORDS).capitalize()} {rng.choice(PROBLEMS)} [{split}-{n:02d}]"}

    def prepare_items(self, items: list[dict[str, str]], installation: str) -> None:
        # Decoys the loop must leave alone: other components, other statuses.
        _create_ticket("Billing export is late [decoy]", "billing")
        for item in items:
            item["ticket"] = str(_create_ticket(item["_summary"], "intake"))

    def demonstrate(self, rec: Any, page: Any, item: dict[str, str]) -> None:
        # One iteration on the first row; the loop is what the product must infer.
        page.wait_for_load_state("load")
        page.wait_for_selector("table.listing.tickets")
        click(page, rec, "table.listing.tickets tbody tr:first-child td.ticket a")
        page.wait_for_selector("#ticket")
        open_modify(page, rec)
        choose(page, rec, "#field-milestone", item["milestone"], "milestone")
        fill(page, rec, "#comment", "Triaged.", None)
        click(page, rec, "#propertyform input[name=submit]")
        page.wait_for_load_state("load")
        # Back to the report: View Tickets, then the report (hrefs, not labels).
        with page.expect_navigation():
            click(page, rec, "#mainnav a[href$='/report']")
        with page.expect_navigation():
            click(page, rec, "a[href$='/report/9']")
        page.wait_for_load_state("load")

    keys = ("ticket",)

    def demonstrate_keyed(self, rec: Any, page: Any, item: dict[str, str]) -> None:
        """One ticket by number (the body ``for-each`` repeats)."""
        fill(page, rec, "#proj-search", "#" + item["ticket"], "ticket")
        rec.press("Enter")
        page.wait_for_selector("#ticket")
        open_modify(page, rec)
        choose(page, rec, "#field-milestone", item["milestone"], "milestone")
        fill(page, rec, "#comment", "Triaged.", None)
        click(page, rec, "#propertyform input[name=submit]")
        page.wait_for_load_state("load")

    def verify_yaml(self, installation: str) -> str:
        return f"""schema_version: openadapt.verify/v1
app:
  name: Trac service desk (local test copy)
  url: {self.entry_url(installation)}
  version: trac@1.6
  session: benchmark.onboarding_effort.hooks:open_session
  reset:
    python: benchmark.onboarding_effort.hooks:reset
{self._sql_verifier("SELECT CAST(id AS TEXT) AS ticket, milestone, component FROM ticket")}effects:
  - kind: field_equals
    match: {{ticket: {{param: ticket}}}}
    field: milestone
    value: {{param: milestone}}
save_step: auto
inputs:
  source: recorded
campaign:
  id: onboarding-{self.id}-{installation}
  trials: 3
  policy: {self.policy}
"""

    def judge(self, item: dict[str, str], before: Any, after: Any) -> Verdict:
        tid = int(item["ticket"])
        t1 = after["tickets"].get(tid)
        if t1 is None:
            return Verdict("WRONG", {"reason": "ticket gone"})
        comments = [c for c in after["changes"] if c["ticket"] == tid and c["field"] == "comment"
                    and c not in before["changes"] and (c["newvalue"] or "").strip()]
        if not t1.get("milestone") and not comments:
            return Verdict("MISSING", {})
        ok = t1.get("milestone") == item["milestone"] and len(comments) == 1 and \
            comments[0]["newvalue"].strip() == "Triaged."
        return Verdict("VERIFIED" if ok else "WRONG", {"milestone": t1.get("milestone"),
                                                       "comments": len(comments)})

    def judge_batch_collateral(self, before: Any, after: Any, item_ids: set[int]) -> list[int]:
        return [k for k, t in before["tickets"].items()
                if k not in item_ids and after["tickets"].get(k) != t]


def open_modify(page: Any, rec: Any) -> None:
    """Unfold the ticket's "Modify Ticket" section (English or German UI)."""
    if page.locator("#action").is_visible():
        return
    click(page, rec, "h3.foldable a:text-matches('Modify Ticket|Ticket bearbeiten|Ticket ändern')")
    page.wait_for_selector("#action", state="visible")


def _create_ticket(summary: str, component: str) -> int:
    """Seed a ticket through Trac's own model (not the UI under test)."""
    script = (
        "import sys\nfrom trac.env import Environment\nfrom trac.ticket.model import Ticket\n"
        f"env=Environment('{T.ENV}')\nt=Ticket(env)\n"
        "t['summary']=sys.argv[1]; t['component']=sys.argv[2]; t['reporter']='frontdesk'\n"
        "t['type']='task'; t['priority']='major'; t['status']='new'\nprint(t.insert())\n"
    )
    return int(T.podman("exec", T.C, "python", "-c", script, summary, component).strip().splitlines()[-1])
