"""Okelund Casualty AgentNet: legacy personal-auto rating wizard.

A frameset (banner, navigation, main) around server-rendered POST-per-page
forms laid out in tables. The ZIP code fills City and State from a ZIP
service as it is typed. The vehicle is chosen in a separate lookup window
(``window.open``) that writes the make and model back into the form. When
the session ends, the main frame shows the sign-in page.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .common import BasePortal, Request, Response, esc, parse_date, parse_int

MARITAL = ["Single", "Married", "Divorced", "Widowed"]
LIC_STATES = ["AZ", "ID", "MT", "NM", "NV", "UT", "WY"]
COUNTS = [("0", "0"), ("1", "1"), ("2", "2"), ("3", "3+")]
YEARS = [str(y) for y in range(2026, 2011, -1)]
USE = ["Commute", "Pleasure", "Business"]
OWNERSHIP = ["Owned", "Financed", "Leased"]
BI = ["25/50", "50/100", "100/300", "250/500"]
DED = [("250", "$250"), ("500", "$500"), ("1000", "$1,000")]
RENTAL = [("None", "None"), ("30", "$30/day, 30 days max"), ("50", "$50/day, 30 days max")]
YN = [("Y", "Yes"), ("N", "No")]

# The vehicle lookup service. Order within a make is the service's own
# (most-quoted first), not alphabetical.
CATALOG: dict[str, list[str]] = {
    "Arvonne": ["Talus Sport", "Talus", "Meridian", "Meridian Hybrid"],
    "Quellen": ["Ostro LX", "Ostro", "Vale", "Vale Touring"],
    "Sarrow": ["Kite", "Kite Wagon", "Harrier"],
    "Torvane": ["Ridge 4 Hybrid", "Ridge 4", "Ridge 6", "Bolt"],
}
BODY = {"Talus Sport": "2D Coupe", "Talus": "4D Sedan", "Meridian": "4D SUV", "Meridian Hybrid": "4D SUV",
        "Ostro LX": "4D Sedan", "Ostro": "4D Sedan", "Vale": "4D Hatchback", "Vale Touring": "4D Wagon",
        "Kite": "4D Hatchback", "Kite Wagon": "4D Wagon", "Harrier": "4D SUV", "Ridge 4 Hybrid": "4D Pickup",
        "Ridge 4": "4D Pickup", "Ridge 6": "4D Pickup", "Bolt": "2D Coupe"}

# page, title, fields: (name, label, kind, options)
PAGES: dict[int, tuple[str, list[tuple[str, str, str, Any]]]] = {
    1: ("Named Insured", [
        ("first_name", "First Name", "text", None),
        ("last_name", "Last Name", "text", None),
        ("dob", "Date of Birth (MM/DD/YYYY)", "date", None),
        ("marital_status", "Marital Status", "select", [(m, m) for m in MARITAL]),
        ("email", "E-Mail Address", "text", None),
        ("street", "Garaging Street", "text", None),
        ("city", "Garaging City", "text", None),
        ("state", "Garaging State", "readonly", None),
        ("zip", "Garaging ZIP", "zip", None),
    ]),
    2: ("Driver", [
        ("license_state", "License State", "select", [(s, s) for s in LIC_STATES]),
        ("years_licensed", "Years Licensed", "text", None),
        ("accidents", "At-Fault Accidents (3 Yrs)", "select", COUNTS),
        ("violations", "Moving Violations (3 Yrs)", "select", COUNTS),
        ("driver_training", "Completed a state-approved driver training course?", "radio", YN),
    ]),
    3: ("Vehicle", [
        ("vehicle_year", "Model Year", "select", [(y, y) for y in YEARS]),
        ("make", "Make", "vehicle", None),
        ("model", "Model", "vehicle", None),
        ("use", "Primary Use", "select", [(u, u) for u in USE]),
        ("annual_miles", "Annual Miles", "text", None),
        ("ownership", "Ownership", "select", [(o, o) for o in OWNERSHIP]),
        ("aftermarket", "Any custom or aftermarket equipment (wheels, audio, lift kits)?", "radio", YN),
    ]),
    4: ("Coverages", [
        ("bi_limits", "Bodily Injury Limits", "select", [(b, b) for b in BI]),
        ("coll_ded", "Collision Deductible", "select", DED),
        ("comp_ded", "Comprehensive Deductible", "select", DED),
        ("rental", "Rental Reimbursement", "select", RENTAL),
        ("eff_date", "Policy Effective Date (MM/DD/YYYY)", "date", None),
    ]),
}
INT_FIELDS = {"years_licensed", "annual_miles", "vehicle_year"}
RECORD_FIELDS = [name for _p, (_t, fs) in sorted(PAGES.items()) for name, *_ in fs]


def _training_applies(d: dict[str, Any]) -> bool:
    y = parse_int(d.get("years_licensed"))
    return y is not None and y < 3


CSS = """
body{font-family:Tahoma,Verdana,sans-serif;font-size:12px;margin:0;background:#ffffff;color:#000}
table.frm{border:1px solid #5d6d4e;border-collapse:collapse;background:#fbfbf3;min-width:620px}
table.frm th{background:#e6e8d4;text-align:right;font-weight:bold;padding:4px 8px;width:300px;border-bottom:1px solid #d3d6bd}
table.frm td{padding:4px 8px;border-bottom:1px solid #d3d6bd}
table.frm caption{background:#5d6d4e;color:#fff;text-align:left;padding:4px 8px;font-weight:bold}
.err{color:#c00000;font-weight:bold}.info{background:#ffffcc;border:1px solid #cc9;padding:4px 8px;margin:6px 0;width:620px}
input.ro{background:#eeeeee;border:1px solid #aaa}
"""


def _doc(title: str, body: str) -> str:
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{esc(title)} - Okelund AgentNet</title>"
            f"<style>{CSS}</style></head><body style='padding:8px 12px'>{body}</body></html>")


class Okelund(BasePortal):
    portal_id = "okelund"
    cookie = "ANSESS"

    def fresh_draft(self) -> dict[str, Any]:
        d: dict[str, Any] = {name: "" for name in RECORD_FIELDS}
        d.update(coll_ded="500", comp_ded="500", rental="None")
        return d

    # ------------------------------------------------------------------
    def route(self, req: Request) -> Response:
        p = req.path
        if p in ("/", "/an"):
            return Response.redirect("/an/desk")
        if p == "/an/login":
            return self.login(req)
        if p == "/an/zip":
            return self.zip_lookup(req.query.get("z", ""))
        if not self.has_session(req):
            nxt = p + ("?" + "&".join(f"{k}={quote(v)}" for k, v in req.query.items()) if req.query else "")
            return Response.redirect("/an/login?next=" + quote(nxt))
        if p == "/an/desk":
            return Response.html(
                "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Okelund Casualty AgentNet</title></head>"
                "<frameset rows='46,*' border='0'><frame name='banner' src='/an/banner' scrolling='no' noresize>"
                "<frameset cols='170,*'><frame name='nav' src='/an/nav'><frame name='main' src='/an/app?pg=1'>"
                "</frameset></frameset></html>")
        if p == "/an/banner":
            return Response.html("<html><body style='margin:0;background:#3d4a33;color:#f0f0dc;font:bold 16px Tahoma;padding:12px'>"
                                 "OKELUND CASUALTY &nbsp;<span style='font-weight:normal;font-size:11px'>AgentNet Personal Auto 6.1</span></body></html>")
        if p == "/an/nav":
            links = "".join(f"<div style='margin:6px 0'><a target='main' href='/an/app?pg={n}'>{n}. {t}</a></div>"
                            for n, (t, _f) in sorted(PAGES.items()))
            return Response.html(f"<html><body style='font:12px Tahoma;background:#eef0e2;padding:8px'><b>Auto Quote</b>{links}"
                                 "<div style='margin:6px 0'><a target='main' href='/an/app?pg=review'>5. Review</a></div></body></html>")
        if p == "/an/app":
            pg = req.query.get("pg", "1")
            if pg == "review":
                return self.review()
            if pg == "done":
                return self.confirm(req.query.get("q", ""))
            n = parse_int(pg)
            if n not in PAGES:
                return Response.html(_doc("Not found", "Not found"), 404)
            return self.page_post(n, req) if req.method == "POST" else self.page_get(n, [])
        if p == "/an/submit" and req.method == "POST":
            return self.submit()
        if p == "/an/vehsearch":
            return self.vehsearch(req)
        return Response.html(_doc("Not found", "Not found"), 404)

    def login(self, req: Request) -> Response:
        nxt = req.query.get("next") or req.form.get("next") or "/an/desk"
        if not nxt.startswith("/"):
            nxt = "/an/desk"
        msg = ""
        if req.method == "POST":
            if self.check_login(req.form.get("agentcode", ""), req.form.get("pwd", "")):
                return Response.redirect(nxt).set_cookie(self.cookie, self.new_session())
            msg = "<p class='err'>Agent Code or Password is incorrect.</p>"
        elif req.query.get("expired"):
            msg = "<p class='err'>Your AgentNet session has timed out. Log in again to resume this quote.</p>"
        body = f"""<h3>AgentNet Log In</h3>{msg}<form method="post" action="/an/login" target="_self">
<input type="hidden" name="next" value="{esc(nxt)}"><table class="frm"><caption>Log In</caption>
<tr><th>Agent Code:</th><td><input name="agentcode" size="20"></td></tr>
<tr><th>Password:</th><td><input name="pwd" type="password" size="20"></td></tr>
<tr><th></th><td><input type="submit" value="Log In"></td></tr></table></form>"""
        return Response.html(_doc("Log In", body))

    def zip_lookup(self, z: str) -> Response:
        hit = self.config.get("zip_table", {}).get(z.strip())
        if not hit:
            return Response.json_({"found": False})
        state, cities = hit
        return Response.json_({"found": True, "state": state, "city": cities[0], "cities": cities})

    # ------------------------------------------------------------------
    def _shown(self, name: str) -> bool:
        if name == "driver_training":
            return _training_applies(self.draft)
        return True

    def _ctl(self, name: str, kind: str, opts: Any) -> str:
        v = self.draft.get(name, "")
        if kind == "date" and v:
            y, m, d = str(v).split("-")
            v = f"{m}/{d}/{y}"
        if kind == "select":
            o = "<option value=''>-- Select --</option>" + "".join(
                f"<option value='{esc(ov)}'{' selected' if str(v) == ov else ''}>{esc(ot)}</option>" for ov, ot in opts)
            return f"<select name='{name}'>{o}</select>"
        if kind == "radio":
            return " ".join(
                f"<label><input type='radio' name='{name}' value='{ov}'{' checked' if v == ov else ''}>{ot}</label>"
                for ov, ot in opts)
        if kind == "readonly":
            return f"<input class='ro' name='{name}' value='{esc(v)}' readonly size='4'>"
        if kind == "vehicle":
            extra = (" <input type='button' value='Vehicle Lookup...' onclick='okLookup()'>" if name == "model" else "")
            return f"<input class='ro' name='{name}' value='{esc(v)}' readonly size='22'>{extra}"
        if kind == "zip":
            return f"<input name='{name}' value='{esc(v)}' size='7' maxlength='5' oninput='okZip(this)'>"
        size = 30 if name in ("email", "street") else 14
        return f"<input name='{name}' value='{esc(v)}' size='{size}'>"

    def page_get(self, n: int, errors: list[str]) -> Response:
        title, fields = PAGES[n]
        rows = ""
        for name, label, kind, opts in fields:
            hide = "" if self._shown(name) else " style='display:none'"
            err = " <span class='err'>Required</span>" if name in errors else ""
            rows += f"<tr id='r_{name}'{hide}><th>{esc(label)}:</th><td>{self._ctl(name, kind, opts)}{err}</td></tr>"
        note = "<p class='err'>Complete the fields marked Required.</p>" if errors else ""
        back = f"<input type='button' value='&lt; Previous' onclick=\"location.href='/an/app?pg={n - 1}'\"> " if n > 1 else ""
        script = """<script>
var okPending = null;
function okZip(el){
  if(!/^\\d{5}$/.test(el.value)) return;
  var f = document.forms.app;
  okPending = fetch('/an/zip?z=' + el.value).then(function(r){return r.json();}).then(function(j){
    if(j.found){ f.elements.city.value = j.city; f.elements.state.value = j.state; }
    okPending = null;
  });
}
function okTraining(){
  var f = document.forms.app; if(!f.elements.years_licensed) return;
  var y = parseInt(f.elements.years_licensed.value, 10);
  document.getElementById('r_driver_training').style.display = (!isNaN(y) && y < 3) ? '' : 'none';
}
function okLookup(){
  var f = document.forms.app;
  window.open('/an/vehsearch?year=' + encodeURIComponent(f.elements.vehicle_year.value), 'oklookup', 'width=620,height=560');
}
function okSetVehicle(make, model){
  var f = document.forms.app; f.elements.make.value = make; f.elements.model.value = model;
}
document.addEventListener('DOMContentLoaded', function(){
  var f = document.forms.app;
  if(f.elements.years_licensed) f.elements.years_licensed.addEventListener('input', okTraining);
  f.addEventListener('submit', function(e){
    if(okPending){ e.preventDefault(); okPending.then(function(){ f.submit(); }); }
  });
});
</script>"""
        body = (f"<h3>{esc(title)}</h3>{note}<form name='app' method='post' action='/an/app?pg={n}'>"
                f"<table class='frm'><caption>{n}. {esc(title)}</caption>{rows}</table><br>"
                f"{back}<input type='submit' value='Save &amp; Next &gt;'></form>{script}")
        return Response.html(_doc(title, body))

    def page_post(self, n: int, req: Request) -> Response:
        _title, fields = PAGES[n]
        with self.lock:
            for name, _label, kind, opts in fields:
                if name not in req.form:
                    continue
                raw = " ".join(req.form.get(name, "").split())
                if kind == "date":
                    self.draft[name] = parse_date(raw) if raw else ""
                elif name in INT_FIELDS:
                    iv = parse_int(raw)
                    self.draft[name] = "" if iv is None else iv
                elif kind in ("select", "radio"):
                    self.draft[name] = raw if raw in [o for o, _t in opts] else ""
                else:
                    self.draft[name] = raw
            if n == 2 and not _training_applies(self.draft):
                self.draft["driver_training"] = ""
            errors = [name for name, *_ in fields if self._shown(name) and self.draft.get(name, "") == ""]
            if errors:
                return self.page_get(n, errors)
            nxt = "/an/app?pg=review" if n == 4 else f"/an/app?pg={n + 1}"
            if self.fire_once(f"post_pg_{n}"):
                for k, v in self.config.get("reset_on_resume", {}).items():
                    self.draft[k] = v
                self.drop_sessions()
                return Response.redirect("/an/login?expired=1&next=" + quote(nxt))
        return Response.redirect(nxt)

    def _display(self, rec: dict[str, Any], name: str, kind: str, opts: Any) -> str:
        v = rec.get(name, "")
        if v == "":
            return "&nbsp;"
        if kind in ("select", "radio"):
            return esc(next((t for o, t in opts if o == str(v)), v))
        if kind == "date":
            y, m, d = str(v).split("-")
            return f"{m}/{d}/{y}"
        return esc(v)

    def _table(self, rec: dict[str, Any], links: bool) -> str:
        out = ""
        for n, (title, fields) in sorted(PAGES.items()):
            edit = f" &nbsp;<a style='color:#fff' href='/an/app?pg={n}'>[Change]</a>" if links else ""
            out += f"<tr><td colspan='2' style='background:#5d6d4e;color:#fff;font-weight:bold'>{esc(title)}{edit}</td></tr>"
            for name, label, kind, opts in fields:
                if name == "driver_training" and rec.get(name, "") == "":
                    continue
                out += f"<tr><th>{esc(label)}:</th><td>{self._display(rec, name, kind, opts)}</td></tr>"
        return out

    def review(self) -> Response:
        body = ("<h3>Review Quote</h3><div class='info'>Confirm each item with the insured. Rated values are final once submitted.</div>"
                f"<table class='frm' id='review'>{self._table(self.draft, True)}</table><br>"
                "<form method='post' action='/an/submit'><input type='button' value='&lt; Previous' "
                "onclick=\"location.href='/an/app?pg=4'\"> <input type='submit' value='Submit for Rating'></form>")
        return Response.html(_doc("Review", body))

    def submit(self) -> Response:
        with self.lock:
            missing = [name for _n, (_t, fs) in PAGES.items() for name, *_ in fs
                       if self._shown(name) and self.draft.get(name, "") == ""]
            if missing:
                return Response.redirect("/an/app?pg=1")
            self.quote_seq += 1
            rec = {k: self.draft.get(k, "") for k in RECORD_FIELDS}
            rec["quote_number"] = f"OC-{60740 + self.quote_seq}"
            self.store_submission(rec)
        return Response.redirect("/an/app?pg=done&q=" + rec["quote_number"])

    def confirm(self, qn: str) -> Response:
        rec = next((r for r in self.submissions if r.get("quote_number") == qn), None)
        if rec is None:
            return Response.html(_doc("Not found", "Quote not found."), 404)
        prem = 640 + 9 * max(0, 10 - int(rec.get("years_licensed") or 0)) * 10 + int(rec.get("annual_miles") or 0) // 100
        body = (f"<h3>Quote Rated</h3><p>Quote Number <b id='quote-number'>{esc(qn)}</b> &nbsp; Six-month premium "
                f"<b>${prem:,}</b></p><p>Values rated:</p><table class='frm' id='rated'>{self._table(rec, False)}</table>")
        return Response.html(_doc("Quote Rated", body))

    # ------------------------------------------------------------------
    def vehsearch(self, req: Request) -> Response:
        q = req.query
        year = q.get("year", "")
        make = q.get("make", "")
        text = " ".join(q.get("model", "").split()).casefold()
        makes = "<option value=''>-- Select --</option>" + "".join(
            f"<option{' selected' if make == m else ''}>{m}</option>" for m in CATALOG)
        rows = ""
        if q.get("go"):
            hits = [m for m in CATALOG.get(make, []) if text in m.casefold()] if year else []
            if hits:
                rows = "<table class='frm' id='results'><caption>Matching vehicles</caption>" + "".join(
                    f"<tr data-model='{esc(m)}'><th>{esc(year)} {esc(make)}</th><td class='m'>{esc(m)}</td>"
                    f"<td>{esc(BODY[m])}</td><td><input type='button' value='Select' "
                    f"onclick=\"okPick('{esc(make)}','{esc(m)}')\"></td></tr>" for m in hits) + "</table>"
            else:
                rows = "<p class='err' id='nohits'>No vehicles match.</p>"
        body = f"""<h3>Vehicle Lookup</h3><form method="get" action="/an/vehsearch"><input type="hidden" name="go" value="1">
<table class="frm"><caption>Search</caption>
<tr><th>Year:</th><td><input class="ro" name="year" value="{esc(year)}" readonly size="5"></td></tr>
<tr><th>Make:</th><td><select name="make">{makes}</select></td></tr>
<tr><th>Model contains:</th><td><input name="model" value="{esc(q.get('model', ''))}" size="18"></td></tr>
<tr><th></th><td><input type="submit" value="Search"></td></tr></table></form><br>{rows}
<script>function okPick(make, model){{ if(window.opener) window.opener.okSetVehicle(make, model); window.close(); }}</script>"""
        return Response.html(_doc("Vehicle Lookup", body))
