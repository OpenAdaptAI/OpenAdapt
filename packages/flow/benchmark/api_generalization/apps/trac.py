"""Trac 1.6 (ticketing, Python, SQLite): a new ticket.

Built from PyPI by ``trac.Containerfile`` (Trac is BSD-3-Clause); nothing is
copied here. Sign-in is HTTP basic auth on /login, which sets Trac's own
session cookie.
"""

from __future__ import annotations

import json
from pathlib import Path
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

C = "apigen-trac"
IMAGE = "localhost/apigen-trac:1.6"
DB = "/srv/trac/db/trac.db"
PASSWORD = "synthetic-admin-pass"  # throwaway local container
QUERY = "SELECT id, summary, description, type, keywords FROM ticket"
JUDGE = "SELECT id, summary, description, type, priority, keywords, reporter, component, status, owner FROM ticket ORDER BY id"


def tsql(query: str, *, write: bool = False) -> list[dict[str, Any]]:
    script = ("import json,sqlite3,sys\n"
              f"c=sqlite3.connect('file:{DB}' + ('' if {write!r} else '?mode=ro'), uri=True, timeout=5)\n"
              "c.row_factory=sqlite3.Row\n"
              "cur=c.execute(sys.argv[1]); rows=[dict(r) for r in cur.fetchall()] if cur.description else []\n"
              "c.commit(); print(json.dumps(rows))\n")
    return json.loads(podman("exec", C, "python", "-c", script, query) or "[]")


class App(AppBase):
    name = "trac"
    workflow = "new-ticket"
    upstream_port = 9871
    proxy_port = 9872
    form_path = "/newticket"
    save_path_regex = r"^/newticket$"
    unique_param = "summary"
    normal_values = [
        dict(type="defect", summary="Label printer skips every other label", description="Seen on the shipping desk unit.", keywords="printer shipping"),
        dict(type="task", summary="Rotate the guest wifi passphrase", description="Quarterly rotation for the lobby network.", keywords="wifi"),
        dict(type="enhancement", summary="Show room capacity on the booking page", description="Facilities asked for this.", keywords="booking"),
    ]
    blank_values = dict(type="defect", summary="Projector remote missing", description="", keywords="")
    dup_values = dict(type="task", summary="Same words", description="Same words", keywords="Same words")
    invalid_values = dict(type="defect", summary="Summary longer than this desk allows: the ticket form takes sixty characters at most",
                          description="Validation case.", keywords="limits")
    invalid_note = "a summary over the desk's 60-character limit (trac.ini max_summary_size; the server rejects it)"
    save_body_exclude = r"(^|&)preview="
    default_column = "priority"
    changed_default_value = "critical"

    def up(self) -> None:
        here = Path(__file__).resolve().parent
        if podman("images", "-q", IMAGE).strip() == "":
            podman("build", "-t", IMAGE, "-f", str(here / "trac.Containerfile"), str(here), timeout=900)
        podman("run", "-d", "--name", C, "-p", f"127.0.0.1:{self.upstream_port}:8000", IMAGE)
        podman("exec", C, "trac-admin", "/srv/trac", "config", "set", "ticket", "max_summary_size", "60")
        self.wait_http("/")

    def down(self) -> None:
        podman("rm", "-f", C, check=False)

    def login(self, page: Any) -> None:
        # Basic auth answers the /login challenge once; Trac then signs the
        # browser in with its own trac_auth cookie.
        import base64

        auth = base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
        page.context.request.get(self.base + "/login", headers={"Authorization": f"Basic {auth}"})
        page.goto(self.base + "/")
        page.wait_for_load_state("load")

    def expire_session(self, page: Any) -> None:
        tsql("DELETE FROM auth_cookie", write=True)

    def record(self, rec: Any, page: Any, v: dict[str, str]) -> None:
        rec_text(rec, page, "#field-summary", v["summary"], "summary")
        rec_text(rec, page, "#field-description", v["description"], "description")
        rec_select(rec, page, "#field-type", v["type"], "type")
        rec_text(rec, page, "#field-keywords", v["keywords"], "keywords")
        rec_click(rec, page, "input[name=submit]")

    def _priority(self, value: str) -> None:
        podman("exec", C, "trac-admin", "/srv/trac", "config", "set", "ticket", "default_priority", value)

    def change_default(self) -> None:
        self._priority("critical")

    def restore_default(self) -> None:
        self._priority("major")

    def rows(self) -> list[dict[str, Any]]:
        return tsql(JUDGE)

    def verifier(self) -> Any:
        return ExecSqlVerifier(tsql, QUERY, timeout_s=10.0)

    def expect_fields(self, v: dict[str, str]) -> dict[str, str]:
        return {"summary": v["summary"], "description": v["description"], "type": v["type"],
                "keywords": v["keywords"], "priority": "major", "reporter": "admin"}

    def effects(self) -> list[Effect]:
        match = {"summary": ValueExpr(param="summary")}
        out = [Effect(kind=EffectKind.RECORD_WRITTEN, match=match, expected_count=1, risk="reversible",
                      probe="exactly one ticket with this summary", timeout_s=8.0)]
        for f in ("description", "type", "keywords"):
            out.append(Effect(kind=EffectKind.FIELD_EQUALS, match=match, field=f, value=ValueExpr(param=f),
                              risk="reversible", probe=f"ticket {f} read-back", timeout_s=8.0))
        return out
