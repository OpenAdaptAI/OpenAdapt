"""Harborline Mutual: legacy agent portal, table layout, server-rendered.

Homeowners quick-rate in four POST-per-step pages, a review page and a
confirmation page. The replacement-cost estimator opens in a separate
window (``window.open``) and writes its figure back into the parent form.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .common import BasePortal, Request, Response, esc, parse_date, parse_int
from .estimators import harborline_rce

STATES = ["AZ", "CA", "CO", "ID", "IL", "MA", "MO", "NV", "OR", "PA", "TN", "TX", "UT", "WA"]
STORIES = [("1", "1"), ("1.5", "1 1/2"), ("2", "2"), ("3", "3")]
CONSTRUCTION = ["Fire Resistive", "Frame", "Masonry", "Masonry Veneer", "Log"]
ROOF = ["Asphalt Shingle", "Architectural Shingle", "Metal", "Clay Tile", "Wood Shake", "Slate"]
LOSSES = [("0", "0"), ("1", "1"), ("2", "2"), ("3", "3 or more")]
DEDUCTIBLES = ["500", "1000", "2500", "5000"]
LIABILITY = ["100000", "300000", "500000"]
QUALITY = ["Economy", "Standard", "Custom", "Premium"]

# name, label, kind, options
STEPS: dict[int, tuple[str, list[tuple[str, str, str, Any]]]] = {
    1: (
        "Applicant Information",
        [
            ("first_name", "First Name", "text", None),
            ("last_name", "Last Name", "text", None),
            ("dob", "Date of Birth (MM/DD/YYYY)", "date", None),
            ("phone", "Phone", "text", None),
            ("email", "E-mail", "text", None),
            ("prior_policy", "Prior Harborline Policy No. (if any)", "text", None),
        ],
    ),
    2: (
        "Risk Location",
        [
            ("street", "Street Address", "text", None),
            ("city", "City", "text", None),
            ("state", "State", "select", [(s, s) for s in STATES]),
            ("zip", "ZIP Code", "text", None),
        ],
    ),
    3: (
        "Dwelling Information",
        [
            ("year_built", "Year Built", "text", None),
            ("sqft", "Total Living Area (sq ft)", "text", None),
            ("stories", "Number of Stories", "select", STORIES),
            ("construction_class", "Construction Class", "select", [(c, c) for c in CONSTRUCTION]),
            ("roof_year", "Roof Covering Last Replaced (Year)", "text", None),
            ("roof_material", "Roof Covering Material", "select", [(r, r) for r in ROOF]),
            ("losses_5yr", "Number of Losses (Last 5 Yrs)", "select", LOSSES),
            ("cov_a", "Dwelling Coverage (A)", "money", None),
        ],
    ),
    4: (
        "Coverage Selections",
        [
            ("eff_date", "Policy Effective Date (MM/DD/YYYY)", "date", None),
            ("deductible", "All Peril Deductible", "select", [(d, f"${int(d):,}") for d in DEDUCTIBLES]),
            ("liability", "Personal Liability (E)", "select", [(v, f"${int(v):,}") for v in LIABILITY]),
            ("pool", "Swimming Pool on Premises?", "radio", [("Y", "Yes"), ("N", "No")]),
            ("pool_fenced", "Is the pool enclosed by a locking fence?", "radio", [("Y", "Yes"), ("N", "No")]),
        ],
    ),
}
OPTIONAL = {"prior_policy"}
INT_FIELDS = {"year_built", "sqft", "roof_year", "cov_a"}
STREET_MAX = 30  # width of the legacy policy-system column

RECORD_FIELDS = [name for _, (_, fields) in sorted(STEPS.items()) for name, *_ in fields]

CSS = """
body{font-family:Verdana,Arial,sans-serif;font-size:12px;background:#d8dde6;margin:0}
#hdr{background:#0b2f5b;color:#fff;padding:6px 10px;font-size:16px;font-weight:bold}
#hdr small{font-weight:normal;font-size:11px;color:#c9d6ea}
table.frm{border-collapse:collapse;background:#fff;border:1px solid #7b8aa3}
table.frm td{padding:4px 8px;border-bottom:1px solid #e3e7ee;vertical-align:top}
td.lbl{background:#eef1f6;text-align:right;font-weight:bold;width:260px}
td.sect{background:#5b7193;color:#fff;font-weight:bold}
.err{color:#b00000;font-weight:bold}
.note{background:#fff7d6;border:1px solid #d9c46b;padding:4px 8px;margin:6px 0;width:640px}
.steps td{padding:2px 10px;border:1px solid #999;background:#eee}
.steps td.on{background:#0b2f5b;color:#fff}
"""


def page(title: str, body: str, step: int | None = None) -> str:
    bar = ""
    if step:
        cells = "".join(
            f"<td class='{'on' if i == step else ''}'>{i}. {STEPS[i][0]}</td>" for i in STEPS
        ) + f"<td class='{'on' if step == 5 else ''}'>5. Review</td>"
        bar = f"<table class='steps' cellspacing=0><tr>{cells}</tr></table><br>"
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{esc(title)} - Harborline Mutual AgentLink</title><style>{CSS}</style></head>"
        "<body><div id='hdr'>HARBORLINE MUTUAL &nbsp;<small>AgentLink Personal Lines Rating v4.2</small></div>"
        f"<div style='padding:10px'>{bar}{body}</div></body></html>"
    )


class Harborline(BasePortal):
    portal_id = "harborline"
    cookie = "HLSESSION"

    def fresh_draft(self) -> dict[str, Any]:
        d = {name: "" for name in RECORD_FIELDS}
        d.update(stories="1", deductible="500", liability="100000")
        d["_prefilled_from"] = ""
        d["_notice"] = ""
        return d

    # ------------------------------------------------------------------
    def route(self, req: Request) -> Response:
        p = req.path
        if p in ("/", "/agent"):
            return Response.redirect("/agent/login")
        if p == "/agent/login":
            return self.login(req)
        if p == "/agent/logout":
            return Response.redirect("/agent/login").clear_cookie(self.cookie)
        if not self.has_session(req):
            nxt = p + ("?" + "&".join(f"{k}={quote(v)}" for k, v in req.query.items()) if req.query else "")
            return Response.redirect("/agent/login?next=" + quote(nxt))
        if p == "/quote/new":
            return Response.redirect("/quote/step/1")
        if p.startswith("/quote/step/"):
            try:
                n = int(p.rsplit("/", 1)[1])
            except ValueError:
                return Response.html(page("Not found", "Not found"), 404)
            if n not in STEPS:
                return Response.html(page("Not found", "Not found"), 404)
            return self.step_post(n, req) if req.method == "POST" else self.step_get(n, [])
        if p == "/quote/review":
            return self.review()
        if p == "/quote/submit" and req.method == "POST":
            return self.submit()
        if p.startswith("/quote/confirm/"):
            return self.confirm(p.rsplit("/", 1)[1])
        if p == "/tools/rce":
            return self.rce(req)
        return Response.html(page("Not found", "<p>Page not found.</p>"), 404)

    def login(self, req: Request) -> Response:
        nxt = req.query.get("next") or req.form.get("next") or "/quote/new"
        if not nxt.startswith("/"):
            nxt = "/quote/new"
        msg = ""
        if req.method == "POST":
            if self.check_login(req.form.get("userid", ""), req.form.get("password", "")):
                sid = self.new_session()
                return Response.redirect(nxt).set_cookie(self.cookie, sid)
            msg = "<p class='err'>Invalid User ID or Password.</p>"
        elif req.query.get("expired"):
            msg = "<p class='err'>Your session has expired. Please sign in again to continue.</p>"
        body = f"""
<h3>Agent Sign In</h3>{msg}
<form method="post" action="/agent/login">
<input type="hidden" name="next" value="{esc(nxt)}">
<table class="frm">
<tr><td class="lbl">User ID</td><td><input type="text" name="userid" size="24"></td></tr>
<tr><td class="lbl">Password</td><td><input type="password" name="password" size="24"></td></tr>
<tr><td></td><td><input type="submit" value="Sign In"></td></tr>
</table></form>"""
        return Response.html(page("Sign In", body))

    # ------------------------------------------------------------------
    def _value_for_display(self, name: str, kind: str) -> str:
        v = self.draft.get(name, "")
        if kind == "date" and v:
            y, m, d = v.split("-")
            return f"{m}/{d}/{y}"
        if kind == "money" and v != "":
            return f"{int(v):,}"
        return str(v)

    def _row(self, name: str, label: str, kind: str, options: Any, errors: list[str]) -> str:
        val = self._value_for_display(name, kind)
        err = " <span class='err'>* Required</span>" if name in errors else ""
        if kind == "select":
            opts = "<option value=''>-- Select --</option>" + "".join(
                f"<option value='{esc(v)}'{' selected' if str(val) == v else ''}>{esc(t)}</option>"
                for v, t in options
            )
            ctl = f"<select name='{name}'>{opts}</select>"
        elif kind == "radio":
            ctl = " ".join(
                f"<input type='radio' name='{name}' value='{v}'{' checked' if val == v else ''}"
                f" onclick='hlToggle()'>{t}"
                for v, t in options
            )
        elif kind == "money":
            ctl = (
                f"$<input type='text' name='{name}' value='{esc(val)}' size='12'> "
                "<input type='button' value='Estimate RC' onclick='hlRce()'>"
            )
        else:
            size = 34 if name in ("street", "email") else 14
            extra = " oninput='hlToggle()' onchange='hlToggle()'" if name == "year_built" else ""
            ctl = f"<input type='text' name='{name}' value='{esc(val)}' size='{size}'{extra}>"
        hidden = ""
        if name == "construction_class" and not self._needs_construction():
            hidden = " style='display:none'"
        if name == "pool_fenced" and self.draft.get("pool") != "Y":
            hidden = " style='display:none'"
        return f"<tr id='row_{name}'{hidden}><td class='lbl'>{esc(label)}:</td><td>{ctl}{err}</td></tr>"

    def _needs_construction(self) -> bool:
        yb = parse_int(self.draft.get("year_built"))
        return yb is not None and yb < 1980

    def step_get(self, n: int, errors: list[str]) -> Response:
        title, fields = STEPS[n]
        rows = "".join(self._row(name, label, kind, opts, errors) for name, label, kind, opts in fields)
        notes = ""
        if self.draft.get("_notice") and n == 1:
            notes += f"<div class='note'>{esc(self.draft['_notice'])}</div>"
        if self.draft.get("_prefilled_from") and n in (2, 3):
            notes += (
                "<div class='note'>Information on this page was copied from policy "
                f"<b>{esc(self.draft['_prefilled_from'])}</b>. Verify it with the applicant.</div>"
            )
        if errors:
            notes += "<p class='err'>Please complete the required fields marked below.</p>"
        back = "<input type='submit' name='nav' value='&lt;&lt; Back'> " if n > 1 else ""
        script = """
<script>
function hlToggle(){
  var f=document.forms['step'];
  if(f.elements['year_built']){var y=parseInt(f.elements['year_built'].value,10);
    document.getElementById('row_construction_class').style.display=(y&&y<1980)?'':'none';}
  if(f.elements['pool']){var on=false;var r=f.elements['pool'];for(var i=0;i<r.length;i++){if(r[i].checked&&r[i].value=='Y')on=true;}
    document.getElementById('row_pool_fenced').style.display=on?'':'none';}
}
function hlRce(){
  var f=document.forms['step'];
  var q='sqft='+encodeURIComponent(f.elements['sqft'].value)+'&yb='+encodeURIComponent(f.elements['year_built'].value)+'&stories='+encodeURIComponent(f.elements['stories'].value);
  window.open('/tools/rce?'+q,'hlrce','width=560,height=520');
}
</script>"""
        body = (
            f"<h3>{esc(title)}</h3>{notes}<form name='step' method='post' action='/quote/step/{n}'>"
            f"<table class='frm'><tr><td class='sect' colspan='2'>{esc(title)}</td></tr>{rows}</table><br>"
            f"{back}<input type='submit' name='nav' value='Next &gt;&gt;'></form>{script}"
        )
        return Response.html(page(title, body, step=n))

    def step_post(self, n: int, req: Request) -> Response:
        _, fields = STEPS[n]
        going_back = "Back" in req.form.get("nav", "")
        with self.lock:
            for name, _label, kind, _opts in fields:
                if name not in req.form:
                    continue
                raw = req.form.get(name, "").strip()
                if kind == "date":
                    self.draft[name] = parse_date(raw) if raw else ""
                elif name in INT_FIELDS:
                    iv = parse_int(raw)
                    self.draft[name] = "" if iv is None else iv
                elif name == "street":
                    self.draft[name] = " ".join(raw.split())[:STREET_MAX]
                else:
                    self.draft[name] = raw
            if n == 1:
                self._maybe_prefill()
            if n == 3 and not self._needs_construction():
                self.draft["construction_class"] = ""
            if n == 4 and self.draft.get("pool") != "Y":
                self.draft["pool_fenced"] = ""
            if going_back:
                return Response.redirect(f"/quote/step/{n - 1}")
            errors = [
                name
                for name, _l, _k, _o in fields
                if name not in OPTIONAL
                and self.draft.get(name, "") == ""
                and not (name == "construction_class" and not self._needs_construction())
                and not (name == "pool_fenced" and self.draft.get("pool") != "Y")
            ]
            if errors:
                return self.step_get(n, errors)
            if self.fire_once(f"post_step_{n}"):
                for k, v in self.config.get("reset_on_resume", {}).items():
                    self.draft[k] = v
                self.drop_sessions()
                nxt = "/quote/review" if n == 4 else f"/quote/step/{n + 1}"
                return Response.redirect("/agent/login?expired=1&next=" + quote(nxt))
        return Response.redirect("/quote/review" if n == 4 else f"/quote/step/{n + 1}")

    def _maybe_prefill(self) -> None:
        prior = self.config.get("prior_policy")
        entered = self.draft.get("prior_policy", "").strip().upper()
        if not entered:
            self.draft["_notice"] = ""
            return
        if not prior or prior["number"].upper() != entered or prior["last_name"].lower() != str(
            self.draft.get("last_name", "")
        ).lower():
            self.draft["_notice"] = f"No policy {entered} was found for this applicant. Continue as new business."
            return
        self.draft["_notice"] = f"Policy {prior['number']} located. Prior risk details were copied to this application."
        if self.draft.get("_prefilled_from") == prior["number"]:
            return
        self.draft["_prefilled_from"] = prior["number"]
        for k, v in prior["fields"].items():
            self.draft[k] = v

    # ------------------------------------------------------------------
    def _display(self, name: str, kind: str, opts: Any) -> str:
        v = self.draft.get(name, "")
        if v == "":
            return "&nbsp;"
        if kind == "select" or kind == "radio":
            for ov, ot in opts:
                if ov == str(v):
                    return esc(ot)
        if kind == "money":
            return f"${int(v):,}"
        if kind == "date":
            y, m, d = v.split("-")
            return f"{m}/{d}/{y}"
        return esc(v)

    def _rows_for_review(self) -> str:
        out = ""
        for n, (title, fields) in sorted(STEPS.items()):
            out += f"<tr><td class='sect' colspan='2'>{esc(title)} &nbsp;<a style='color:#fff' href='/quote/step/{n}'>[Edit]</a></td></tr>"
            for name, label, kind, opts in fields:
                if name == "construction_class" and not self._needs_construction():
                    continue
                if name == "pool_fenced" and self.draft.get("pool") != "Y":
                    continue
                out += f"<tr><td class='lbl'>{esc(label)}:</td><td>{self._display(name, kind, opts)}</td></tr>"
        return out

    def review(self) -> Response:
        body = (
            "<h3>Review Application</h3><p>Verify every item with the applicant before submitting. "
            "Submitted applications cannot be changed online.</p>"
            f"<table class='frm'>{self._rows_for_review()}</table><br>"
            "<form method='get' action='/quote/step/4' style='display:inline'><input type='submit' value='&lt;&lt; Back'></form> "
            "<form method='post' action='/quote/submit' style='display:inline'><input type='submit' value='Submit Application'></form>"
        )
        return Response.html(page("Review", body, step=5))

    def _missing(self) -> list[str]:
        miss = []
        for n, (_t, fields) in STEPS.items():
            for name, _l, _k, _o in fields:
                if name in OPTIONAL:
                    continue
                if name == "construction_class" and not self._needs_construction():
                    continue
                if name == "pool_fenced" and self.draft.get("pool") != "Y":
                    continue
                if self.draft.get(name, "") == "":
                    miss.append(name)
        return miss

    def submit(self) -> Response:
        with self.lock:
            if self._missing():
                return Response.redirect("/quote/step/1")
            self.quote_seq += 1
            record = {k: self.draft.get(k, "") for k in RECORD_FIELDS}
            record["application_no"] = f"HM-{40210 + self.quote_seq}"
            self.store_submission(record)
        return Response.redirect(f"/quote/confirm/{record['application_no']}")

    def confirm(self, app_no: str) -> Response:
        rec = next((r for r in self.submissions if r.get("application_no") == app_no), None)
        if rec is None:
            return Response.html(page("Not found", "<p>Application not found.</p>"), 404)
        rows = ""
        for _n, (_t, fields) in sorted(STEPS.items()):
            for name, label, kind, opts in fields:
                v = rec.get(name, "")
                if v == "":
                    continue
                saved = self.draft.get(name)
                self.draft[name] = v
                rows += f"<tr><td class='lbl'>{esc(label)}:</td><td>{self._display(name, kind, opts)}</td></tr>"
                self.draft[name] = saved
        body = (
            f"<h3>Application Received</h3><p>Application number <b id='appno'>{esc(app_no)}</b> was submitted "
            "for underwriting. Values on file:</p>"
            f"<table class='frm'>{rows}</table>"
        )
        return Response.html(page("Confirmation", body))

    # ------------------------------------------------------------------
    def rce(self, req: Request) -> Response:
        q = req.query
        sqft = parse_int(q.get("sqft"))
        yb = parse_int(q.get("yb"))
        stories = q.get("stories", "1")
        quality = q.get("quality", "")
        result = ""
        if q.get("calc") and sqft and yb and stories in dict(STORIES) and quality in QUALITY:
            value = harborline_rce(sqft, yb, stories, quality)
            # The AgentLink bridge hands large figures back in rounded form.
            handed = int(round(value / 10000.0)) * 10000 if value >= 500000 else value
            result = f"""
<table class="frm"><tr><td class="lbl">Estimated Replacement Cost:</td>
<td><b id="rce_value" style="font-size:15px">${value:,}</b></td></tr></table><br>
<input type="button" value="Use This Value" onclick="useIt()">
<script>
function useIt(){{
  var w=window.opener;
  if(w&&w.document.forms['step']){{w.document.forms['step'].elements['cov_a'].value='{handed:,}';}}
  window.close();
}}
</script>"""
        elif q.get("calc"):
            result = "<p class='err'>Complete all inputs to calculate.</p>"
        qopts = "".join(
            f"<option{' selected' if (quality or 'Standard') == g else ''}>{g}</option>" for g in QUALITY
        )
        sopts = "".join(
            f"<option value='{v}'{' selected' if stories == v else ''}>{t}</option>" for v, t in STORIES
        )
        body = f"""
<h3>Replacement Cost Estimator</h3>
<form method="get" action="/tools/rce">
<input type="hidden" name="calc" value="1">
<table class="frm">
<tr><td class="lbl">Living Area (sq ft):</td><td><input type="text" name="sqft" value="{esc(sqft or '')}" size="10"></td></tr>
<tr><td class="lbl">Year Built:</td><td><input type="text" name="yb" value="{esc(yb or '')}" size="6"></td></tr>
<tr><td class="lbl">Stories:</td><td><select name="stories">{sopts}</select></td></tr>
<tr><td class="lbl">Quality Grade:</td><td><select name="quality">{qopts}</select></td></tr>
<tr><td></td><td><input type="submit" value="Calculate"></td></tr>
</table></form><br>{result}"""
        return Response.html(page("Replacement Cost Estimator", body))
