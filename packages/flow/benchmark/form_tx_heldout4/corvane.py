"""Corvane Casualty: motorcycle quote in an agency rating worksheet.

One server-rendered worksheet with accordion panels; each panel saves on its
own through a JSON call. Motorcycles are added in a modal dialog whose VIN
decoder opens a separate popup window that writes the decoded vehicle back
into the dialog and closes itself. The coverage panel has a custom parts and
equipment worksheet dialog. Rating shows a rated-quote page before the
application is submitted. When the session ends, the worksheet sends the
agent to the sign-in page.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .common import BasePortal, Request, Response, esc, money, parse_date, parse_int

RECORD_FIELDS = [
    "effective_date", "prior_insurance",
    "first_name", "last_name", "dob", "phone", "years_endorsed", "safety_course",
    "street", "city", "state", "zip",
    "motorcycles", "annual_miles",
    "bi_pd_limits", "collision_deductible", "comprehensive_deductible", "cpe_coverage", "roadside",
]
ITEM_FIELDS = ["vin", "year", "make", "model", "engine_cc", "ownership", "lienholder"]

PRIOR = ["None", "Less than 6 months", "6 months or more"]
YES_NO = ["Yes", "No"]
STATES = ["WA", "OR", "ID", "MT", "CA"]
LIMITS = ["25/50/25", "50/100/50", "100/300/100", "250/500/100"]
COLLISION = [250, 500, 1000]
COMPREHENSIVE = [100, 250, 500, 1000]
ROADSIDE = ["Not included", "Roadside assistance", "Roadside and trip interruption"]
OWNERSHIP = ["Owned outright", "Financed", "Leased"]

# VIN decoder table: several VINs decode to more than one trim.
VINS: dict[str, list[dict[str, Any]]] = {
    "KS9RB0905NA214470": [
        {"year": 2022, "make": "Kessin", "model": "Ridgeback 900", "engine_cc": 898},
        {"year": 2022, "make": "Kessin", "model": "Ridgeback 900 Touring", "engine_cc": 898},
    ],
    "AR7TL1272MB300982": [{"year": 2021, "make": "Ardent Cycle Co.", "model": "Tallis 1250", "engine_cc": 1252}],
    "SM2CV6504PC118837": [{"year": 2023, "make": "Strand Motorworks", "model": "Cove 650", "engine_cc": 649}],
    "KS9DT7003LA552019": [{"year": 2020, "make": "Kessin", "model": "Drift 700", "engine_cc": 689}],
    "VH4MR3109RE071266": [{"year": 2024, "make": "Vahlberg", "model": "Marrow 310", "engine_cc": 313}],
    "AR7HW1803KB447105": [
        {"year": 2019, "make": "Ardent Cycle Co.", "model": "Highwater 1800", "engine_cc": 1801},
        {"year": 2019, "make": "Ardent Cycle Co.", "model": "Highwater 1800 Limited", "engine_cc": 1801},
    ],
    "SM2PK4002SC209318": [{"year": 2025, "make": "Strand Motorworks", "model": "Pike 400", "engine_cc": 399}],
    "VH4GR9500NE630274": [{"year": 2022, "make": "Vahlberg", "model": "Grisle 950", "engine_cc": 948}],
    "KS9SL1104TA017763": [{"year": 2026, "make": "Kessin", "model": "Sable 1100", "engine_cc": 1084}],
    "AR7BN8806JB905531": [{"year": 2018, "make": "Ardent Cycle Co.", "model": "Benton 880", "engine_cc": 883}],
    "SM2RV1205MC388460": [{"year": 2021, "make": "Strand Motorworks", "model": "Raven 1200", "engine_cc": 1170}],
    "VH4TK6907PE215598": [{"year": 2023, "make": "Vahlberg", "model": "Tarka 690", "engine_cc": 693}],
    "KS9CL5003RA884210": [{"year": 2024, "make": "Kessin", "model": "Coil 500", "engine_cc": 471}],
    "VH4WL8003SE450917": [{"year": 2025, "make": "Vahlberg", "model": "Wilder 800", "engine_cc": 798}],
}

LABELS = {
    "effective_date": "Effective date", "prior_insurance": "Prior motorcycle insurance",
    "first_name": "First name", "last_name": "Last name", "dob": "Date of birth", "phone": "Phone",
    "years_endorsed": "Years with motorcycle endorsement",
    "safety_course": "Completed a rider safety course in the last 3 years",
    "street": "Street address", "city": "City", "state": "State", "zip": "ZIP",
    "annual_miles": "Annual mileage", "bi_pd_limits": "Liability limits (BI/PD)",
    "collision_deductible": "Collision deductible", "comprehensive_deductible": "Comprehensive deductible",
    "cpe_coverage": "Custom parts and equipment", "roadside": "Roadside coverage",
}
PANELS = {
    "basics": ("Quote basics", ["effective_date", "prior_insurance"]),
    "rider": ("Rider", ["first_name", "last_name", "dob", "phone", "years_endorsed", "safety_course"]),
    "garaging": ("Garaging address", ["street", "city", "state", "zip"]),
    "motorcycles": ("Motorcycles", ["motorcycles", "annual_miles"]),
    "coverage": ("Coverage", ["bi_pd_limits", "collision_deductible", "comprehensive_deductible", "cpe_coverage",
                              "roadside"]),
}


def _us(iso: Any) -> str:
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(iso or ""))
    return f"{m.group(2)}/{m.group(3)}/{m.group(1)}" if m else str(iso or "")


class Corvane(BasePortal):
    portal_id = "corvane"
    cookie = "cv_session"

    def fresh_draft(self) -> dict[str, Any]:
        d: dict[str, Any] = {f: "" for f in RECORD_FIELDS}
        d.update(motorcycles=[], annual_miles=6000, bi_pd_limits="25/50/25", collision_deductible=500,
                 comprehensive_deductible=500, cpe_coverage=0, roadside="Not included")
        d.update(copy_json(self.config.get("prefill", {})))
        d["_notice"] = ""
        d["_entered_city"] = ""
        d["_kept_address"] = False
        return d

    # ---- validation --------------------------------------------------
    def _clean_bike(self, b: dict[str, Any]) -> tuple[dict[str, Any], str]:
        vin = str(b.get("vin", "")).strip().upper()
        cands = VINS.get(vin)
        if not cands:
            return {}, f"VIN {vin or '(blank)'} was not decoded."
        match = next((c for c in cands if c["model"] == b.get("model")), None)
        if match is None:
            return {}, f"Decode VIN {vin} again."
        own = str(b.get("ownership", ""))
        if own not in OWNERSHIP:
            return {}, "Choose the ownership."
        lien = str(b.get("lienholder", "") or "").strip()
        if own == "Owned outright":
            lien = ""
        elif not lien:
            return {}, "Enter the lienholder or lessor."
        return {"vin": vin, "year": match["year"], "make": match["make"], "model": match["model"],
                "engine_cc": match["engine_cc"], "ownership": own, "lienholder": lien}, ""

    def _clean(self, panel: str, data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        out: dict[str, Any] = {}
        err: dict[str, str] = {}

        def s(f: str) -> str:
            v = data.get(f, "")
            return "" if v is None else str(v).strip()

        if panel == "basics":
            iso = parse_date(s("effective_date"))
            out["effective_date"] = iso
            if not iso:
                err["effective_date"] = "Enter MM/DD/YYYY."
            out["prior_insurance"] = s("prior_insurance")
            if out["prior_insurance"] not in PRIOR:
                err["prior_insurance"] = "Required."
        elif panel == "rider":
            for f in ("first_name", "last_name"):
                out[f] = s(f)
                if not out[f]:
                    err[f] = "Required."
            out["dob"] = parse_date(s("dob"))
            if not out["dob"]:
                err["dob"] = "Enter MM/DD/YYYY."
            out["phone"] = s("phone")
            if len(re.sub(r"\D", "", out["phone"])) != 10:
                err["phone"] = "10 digits."
            yrs = parse_int(s("years_endorsed"))
            out["years_endorsed"] = yrs
            if yrs is None or not 0 <= yrs <= 70:
                err["years_endorsed"] = "Whole years."
            out["safety_course"] = s("safety_course")
            if out["safety_course"] not in YES_NO:
                err["safety_course"] = "Required."
        elif panel == "garaging":
            for f in ("street", "city"):
                out[f] = s(f)
                if not out[f]:
                    err[f] = "Required."
            out["state"] = s("state")
            if out["state"] not in STATES:
                err["state"] = "Required."
            out["zip"] = s("zip")
            if not re.fullmatch(r"\d{5}", out["zip"]):
                err["zip"] = "5 digits."
        elif panel == "motorcycles":
            bikes = data.get("motorcycles") or []
            clean_bikes = []
            for b in bikes if isinstance(bikes, list) else []:
                cb, e = self._clean_bike(b if isinstance(b, dict) else {})
                if e:
                    err["motorcycles"] = e
                    break
                clean_bikes.append(cb)
            if not clean_bikes and "motorcycles" not in err:
                err["motorcycles"] = "Add at least one motorcycle."
            out["motorcycles"] = clean_bikes
            miles = parse_int(s("annual_miles"))
            out["annual_miles"] = miles
            if miles is None or not 0 < miles <= 60000:
                err["annual_miles"] = "Enter annual miles."
        elif panel == "coverage":
            out["bi_pd_limits"] = s("bi_pd_limits")
            if out["bi_pd_limits"] not in LIMITS:
                err["bi_pd_limits"] = "Required."
            for f, opts in (("collision_deductible", COLLISION), ("comprehensive_deductible", COMPREHENSIVE)):
                out[f] = parse_int(s(f))
                if out[f] not in opts:
                    err[f] = "Required."
            out["cpe_coverage"] = parse_int(s("cpe_coverage"))
            if out["cpe_coverage"] is None or out["cpe_coverage"] < 0:
                err["cpe_coverage"] = "Whole dollars."
            out["roadside"] = s("roadside")
            if out["roadside"] not in ROADSIDE:
                err["roadside"] = "Required."
        return out, err

    def _premium(self, d: dict[str, Any]) -> int:
        p = 140.0
        for b in d.get("motorcycles") or []:
            p += 90 + int(b.get("engine_cc") or 0) * 0.21
        p *= {"25/50/25": 1.0, "50/100/50": 1.18, "100/300/100": 1.36, "250/500/100": 1.58}.get(d.get("bi_pd_limits"), 1)
        p *= {250: 1.12, 500: 1.0, 1000: 0.9}.get(d.get("collision_deductible"), 1)
        p += int(d.get("cpe_coverage") or 0) * 0.012
        p += int(d.get("annual_miles") or 0) * 0.008
        if d.get("safety_course") == "Yes":
            p *= 0.93
        p += {"Not included": 0, "Roadside assistance": 24, "Roadside and trip interruption": 41}.get(d.get("roadside"), 0)
        return int(round(p))

    def _display(self, f: str, v: Any) -> str:
        if v in ("", None):
            return "—"
        if f in ("collision_deductible", "comprehensive_deductible", "cpe_coverage"):
            return money(int(v))
        if f in ("dob", "effective_date"):
            return _us(v)
        if f == "annual_miles":
            return f"{int(v):,}"
        return str(v)

    # ---- routing -----------------------------------------------------
    def route(self, req: Request) -> Response:
        p = req.path
        if p == "/":
            return Response.redirect("/rater/worksheet")
        if p == "/agent/login":
            if req.method == "POST":
                nxt = req.form.get("next") or "/rater/worksheet"
                if not nxt.startswith("/rater/"):
                    nxt = "/rater/worksheet"
                if self.check_login(req.form.get("agent_code", ""), req.form.get("password", "")):
                    return Response.redirect(nxt).set_cookie(self.cookie, self.new_session())
                return Response.html(self._login_page(nxt, "Agent code or password is not valid.", False), 401)
            return Response.html(self._login_page(req.query.get("next", ""), "", req.query.get("expired") == "1"))
        if not p.startswith("/rater/"):
            return Response.html("<h1>Not found</h1>", 404)
        if not self.has_session(req):
            if p.startswith("/rater/api/"):
                return Response.json_({"ok": False, "error": "session_expired"}, 401)
            return Response.redirect("/agent/login?expired=1&next=/rater/worksheet")
        if p == "/rater/worksheet":
            return Response.html(self._worksheet(req.query.get("msg", "")))
        if p == "/rater/vin-decoder":
            return Response.html(self._decoder(req.query.get("vin", "")))
        if p == "/rater/api/save" and req.method == "POST":
            body = req.json or {}
            panel = str(body.get("panel", ""))
            if panel not in PANELS:
                return Response.json_({"ok": False, "error": "bad panel"}, 400)
            if self.fire_once("save:" + panel):
                self.drop_sessions()
                return Response.json_({"ok": False, "error": "session_expired"}, 401)
            clean, err = self._clean(panel, body.get("data") or {})
            if err:
                return Response.json_({"ok": False, "errors": err}, 422)
            with self.lock:
                self.draft.update(clean)
                if panel == "garaging":
                    self.draft["_kept_address"] = False
                    self.draft["_notice"] = ""
            return Response.json_({"ok": True})
        if p == "/rater/rate" and req.method == "POST":
            missing = self._incomplete()
            if missing:
                return Response.redirect("/rater/worksheet?msg=" + "+".join(missing))
            with self.lock:
                std = self.config.get("standardize")
                d = self.draft
                if std and not d["_kept_address"] and d["zip"] == std["zip"] and d["city"].casefold() != std["city"].casefold():
                    d["_entered_city"] = d["city"]
                    d["city"] = std["city"]
                    d["_notice"] = (f"Garaging address standardized to the USPS preferred city for ZIP {std['zip']}: "
                                    f"{d['_entered_city']} changed to {std['city']}.")
            return Response.redirect("/rater/rated")
        if p == "/rater/keep-address" and req.method == "POST":
            with self.lock:
                d = self.draft
                if d["_entered_city"]:
                    d["city"] = d["_entered_city"]
                d["_entered_city"] = ""
                d["_notice"] = ""
                d["_kept_address"] = True
            return Response.redirect("/rater/rated")
        if p == "/rater/rated":
            return Response.html(self._rated())
        if p == "/rater/submit" and req.method == "POST":
            missing = self._incomplete()
            if missing:
                return Response.redirect("/rater/worksheet?msg=" + "+".join(missing))
            with self.lock:
                rec = {f: copy_json(self.draft[f]) for f in RECORD_FIELDS}
                self.quote_seq += 1
                ref = f"CVQ-{88310 + self.quote_seq}"
            rec["quote_ref"] = ref
            rec["premium"] = self._premium(rec)
            self.store_submission(rec)
            return Response.redirect("/rater/quote/" + ref)
        m = re.fullmatch(r"/rater/quote/(CVQ-\d+)", p)
        if m:
            return Response.html(self._quote(m.group(1)))
        return Response.html("<h1>Not found</h1>", 404)

    def _incomplete(self) -> list[str]:
        with self.lock:
            d = dict(self.draft)
        missing = []
        for key, (title, fields) in PANELS.items():
            data = {f: (_us(d[f]) if f in ("dob", "effective_date") else d[f]) for f in fields}
            _, err = self._clean(key, data)
            if err:
                missing.append(title.replace(" ", "_"))
        return missing

    # ---- pages -------------------------------------------------------
    def _login_page(self, nxt: str, error: str, expired: bool) -> str:
        msg = ""
        if expired:
            msg = '<p class="warn" role="alert">Your session timed out. Sign in to continue rating.</p>'
        if error:
            msg = f'<p class="warn" role="alert">{esc(error)}</p>'
        return PAGE.replace("{{BODY}}", f"""
<div class="login">
<h1>Corvane AgentLink</h1>
{msg}
<form method="post" action="/agent/login">
<input type="hidden" name="next" value="{esc(nxt)}">
<table class="form">
<tr><td><label for="agent_code">Agent code</label></td><td><input id="agent_code" name="agent_code"></td></tr>
<tr><td><label for="password">Password</label></td><td><input id="password" name="password" type="password"></td></tr>
</table>
<button type="submit">Sign in</button>
</form></div>""")

    def _field(self, f: str, d: dict[str, Any], kind: str = "text", options: list[Any] | None = None,
               fmt: Any = None, placeholder: bool = False) -> str:
        v = d.get(f, "")
        if options is not None:
            cur = str(v)
            opts = '<option value="">-- Select --</option>' if placeholder else ""
            for o in options:
                text = fmt(o) if fmt else str(o)
                sel = " selected" if str(o) == cur else ""
                opts += f'<option value="{esc(o)}"{sel}>{esc(text)}</option>'
            ctl = f'<select id="{f}" name="{f}">{opts}</select>'
        else:
            shown = _us(v) if f in ("dob", "effective_date") else ("" if v in (None,) else v)
            ph = ' placeholder="MM/DD/YYYY"' if f in ("dob", "effective_date") else ""
            ctl = f'<input id="{f}" name="{f}" type="{kind}" value="{esc(shown)}"{ph}>'
        return (f'<tr><td class="lbl"><label for="{f}">{esc(LABELS[f])}</label></td><td>{ctl}'
                f'<span class="err" data-err="{f}"></span></td></tr>')

    def _worksheet(self, msg: str) -> str:
        with self.lock:
            d = copy_json({k: v for k, v in self.draft.items() if not k.startswith("_")})
        banner = ""
        if msg:
            banner = (f'<p class="warn" role="alert">Complete these panels before rating: '
                      f'{esc(msg.replace("_", " ").replace("+", ", "))}</p>')

        def panel(key: str, inner: str, extra: str = "") -> str:
            title = PANELS[key][0]
            return f"""
<section class="panel" data-panel="{key}">
<h2><button type="button" class="acc" aria-expanded="false" aria-controls="body-{key}" id="hdr-{key}">{esc(title)}
<span class="saved" data-saved="{key}"></span></button></h2>
<div class="body" id="body-{key}" role="region" aria-labelledby="hdr-{key}" hidden>
<table class="form">{inner}</table>{extra}
<p class="err" data-panel-err="{key}"></p>
<button type="button" class="save" data-save="{key}">Save panel</button>
</div></section>"""

        f = self._field
        basics = f("effective_date", d) + f("prior_insurance", d, options=PRIOR, placeholder=True)
        rider = (f("first_name", d) + f("last_name", d) + f("dob", d) + f("phone", d) + f("years_endorsed", d)
                 + f("safety_course", d, options=YES_NO, placeholder=True))
        garaging = f("street", d) + f("city", d) + f("state", d, options=STATES, placeholder=True) + f("zip", d)
        bikes_extra = """
<table class="grid" id="bike-grid"><thead><tr><th>VIN</th><th>Year</th><th>Make</th><th>Model</th><th>cc</th>
<th>Ownership</th><th>Lienholder</th><th></th></tr></thead><tbody></tbody></table>
<p><button type="button" id="add-bike">Add motorcycle</button></p>"""
        bikes = f("annual_miles", d)
        coverage = (f("bi_pd_limits", d, options=LIMITS)
                    + f("collision_deductible", d, options=COLLISION, fmt=lambda o: money(o))
                    + f("comprehensive_deductible", d, options=COMPREHENSIVE, fmt=lambda o: money(o))
                    + f("cpe_coverage", d)
                    + '<tr><td></td><td><button type="button" id="open-cpe">CPE worksheet</button></td></tr>'
                    + f("roadside", d, options=ROADSIDE))
        body = f"""
<div class="bar">Corvane AgentLink &rsaquo; Personal lines &rsaquo; <b>Motorcycle rating worksheet</b>
<span class="right">Agent {esc(self.creds['username'])}</span></div>
<div class="ws">
<div id="banner">{banner}</div>
{panel('basics', basics)}
{panel('rider', rider)}
{panel('garaging', garaging)}
{panel('motorcycles', bikes, bikes_extra)}
{panel('coverage', coverage)}
<form method="post" action="/rater/rate" class="rate"><button type="submit" id="rate">Rate this quote</button></form>
</div>
<dialog id="bike" aria-labelledby="bike-h">
<h3 id="bike-h">Add motorcycle</h3>
<table class="form">
<tr><td class="lbl"><label for="m-vin">VIN</label></td><td><input id="m-vin" maxlength="17" style="width:190px">
<button type="button" id="decode">Decode VIN</button></td></tr>
<tr><td class="lbl"><label for="m-year">Model year</label></td><td><input id="m-year" readonly></td></tr>
<tr><td class="lbl"><label for="m-make">Make</label></td><td><input id="m-make" readonly></td></tr>
<tr><td class="lbl"><label for="m-model">Model</label></td><td><input id="m-model" readonly></td></tr>
<tr><td class="lbl"><label for="m-cc">Engine (cc)</label></td><td><input id="m-cc" readonly></td></tr>
<tr><td class="lbl"><label for="m-own">Ownership</label></td><td><select id="m-own"><option value="">-- Select --</option>
{''.join(f'<option>{o}</option>' for o in OWNERSHIP)}</select></td></tr>
<tr id="lien-row" hidden><td class="lbl"><label for="m-lien">Lienholder name</label></td><td><input id="m-lien"></td></tr>
</table>
<p class="err" id="bike-err"></p>
<button type="button" id="bike-ok">Add to quote</button> <button type="button" id="bike-cancel">Cancel</button>
</dialog>
<dialog id="cpe" aria-labelledby="cpe-h">
<h3 id="cpe-h">Custom parts and equipment worksheet</h3>
<table class="grid" id="cpe-grid"><thead><tr><th>Item</th><th>Value ($)</th></tr></thead><tbody></tbody></table>
<p><button type="button" id="cpe-add">Add item</button></p>
<p id="cpe-total">Worksheet total: $0</p>
<button type="button" id="cpe-apply">Apply total</button> <button type="button" id="cpe-close">Close</button>
</dialog>
<script>
const BIKES = {json.dumps(d['motorcycles'])};
</script>
{WORKSHEET_JS}"""
        return PAGE.replace("{{BODY}}", body)

    def _decoder(self, vin: str) -> str:
        vin = vin.strip().upper()
        cands = VINS.get(vin, [])
        if not cands:
            inner = f"<p>No match for VIN <b>{esc(vin)}</b>. Check the VIN and try again.</p>"
        else:
            rows = ""
            for i, c in enumerate(cands):
                rows += (f"<tr><td>{c['year']}</td><td>{esc(c['make'])}</td><td class='model'>{esc(c['model'])}</td>"
                         f"<td>{c['engine_cc']}</td><td><button type='button' data-pick='{i}'>Use this vehicle</button>"
                         f"</td></tr>")
            inner = f"""<p>{len(cands)} match{'es' if len(cands) != 1 else ''} for VIN <b>{esc(vin)}</b>.</p>
<table class="grid"><thead><tr><th>Year</th><th>Make</th><th>Model</th><th>Engine (cc)</th><th></th></tr></thead>
<tbody>{rows}</tbody></table>"""
        return PAGE.replace("{{BODY}}", f"""<div class="ws"><h3>VIN decoder</h3>{inner}</div>
<script>
const CANDS = {json.dumps(cands)}; const VIN = {json.dumps(vin)};
document.querySelectorAll('[data-pick]').forEach(b => b.onclick = () => {{
  const c = CANDS[+b.dataset.pick];
  if (window.opener && window.opener.applyDecoded) window.opener.applyDecoded(Object.assign({{vin: VIN}}, c));
  window.close();
}});
</script>""")

    def _summary_rows(self, d: dict[str, Any]) -> str:
        out = ""
        for key, (title, fields) in PANELS.items():
            out += f'<tr class="grp"><th colspan="2">{esc(title)}</th></tr>'
            for f in fields:
                if f == "motorcycles":
                    continue
                out += f"<tr><td class='lbl'>{esc(LABELS[f])}</td><td>{esc(self._display(f, d.get(f)))}</td></tr>"
        return out

    def _bike_rows(self, d: dict[str, Any]) -> str:
        return "".join(
            f"<tr><td>{esc(b['vin'])}</td><td>{b['year']}</td><td>{esc(b['make'])}</td><td>{esc(b['model'])}</td>"
            f"<td>{b['engine_cc']}</td><td>{esc(b['ownership'])}</td><td>{esc(b['lienholder'] or '—')}</td></tr>"
            for b in d.get("motorcycles") or []
        )

    def _rated(self) -> str:
        with self.lock:
            d = copy_json(self.draft)
        notice = ""
        if d.get("_notice"):
            notice = f"""<div class="warn" role="status" id="std-notice">{esc(d['_notice'])}
<form method="post" action="/rater/keep-address" style="display:inline"><button type="submit">Use address as entered</button></form></div>"""
        return PAGE.replace("{{BODY}}", f"""
<div class="bar">Corvane AgentLink &rsaquo; Motorcycle &rsaquo; <b>Rated quote</b></div>
<div class="ws">
{notice}
<h2>Rated quote</h2>
<table class="form summary" id="rated">{self._summary_rows(d)}</table>
<h3>Motorcycles</h3>
<table class="grid" id="rated-bikes"><thead><tr><th>VIN</th><th>Year</th><th>Make</th><th>Model</th><th>cc</th>
<th>Ownership</th><th>Lienholder</th></tr></thead><tbody>{self._bike_rows(d)}</tbody></table>
<p class="prem">Annual premium: <b id="premium">{esc(money(self._premium(d)))}</b></p>
<p><a href="/rater/worksheet">Back to worksheet</a></p>
<form method="post" action="/rater/submit"><button type="submit" id="submit-app">Submit application</button></form>
</div>""")

    def _quote(self, ref: str) -> str:
        with self.lock:
            rec = next((r for r in self.submissions if r.get("quote_ref") == ref), None)
        if rec is None:
            return "<h1>Quote not found</h1>"
        return PAGE.replace("{{BODY}}", f"""
<div class="bar">Corvane AgentLink &rsaquo; Motorcycle &rsaquo; <b>Application received</b></div>
<div class="ws">
<h2>Application submitted</h2>
<p>Quote reference <b id="quote-ref">{esc(ref)}</b> &nbsp; Annual premium <b id="premium">{esc(money(rec['premium']))}</b></p>
<table class="form summary">{self._summary_rows(rec)}</table>
<h3>Motorcycles</h3>
<table class="grid"><thead><tr><th>VIN</th><th>Year</th><th>Make</th><th>Model</th><th>cc</th>
<th>Ownership</th><th>Lienholder</th></tr></thead><tbody>{self._bike_rows(rec)}</tbody></table>
</div>""")


def copy_json(v: Any) -> Any:
    return json.loads(json.dumps(v))


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Corvane AgentLink</title>
<style>
body{margin:0;font:13px Verdana,Tahoma,sans-serif;background:#e8e8e3;color:#222}
.bar{background:#3c4f66;color:#fff;padding:7px 12px}
.bar .right{float:right;opacity:.85}
.ws{max-width:900px;margin:12px auto;background:#fff;border:1px solid #a9a9a0;padding:12px 16px}
.login{max-width:380px;margin:60px auto;background:#fff;border:1px solid #a9a9a0;padding:14px 18px}
h1{font-size:17px;color:#3c4f66}h2{font-size:14px;margin:0}h3{font-size:13px;color:#3c4f66}
table.form td{padding:3px 6px;vertical-align:top}
td.lbl{width:290px;text-align:right;color:#333}
input,select{font:inherit;padding:2px 3px;border:1px solid #7f8a96}
button{font:inherit;padding:3px 10px;background:#dfe4ea;border:1px solid #6b7785;cursor:pointer}
.panel{border:1px solid #b9c0c8;margin:0 0 6px}
.panel h2 button.acc{display:block;width:100%;text-align:left;background:#d6dde6;border:none;padding:6px 10px;font-weight:bold}
.panel .body{padding:8px 10px}
.saved{color:#1e6b2e;font-weight:normal;margin-left:10px}
.err{color:#b00000;margin-left:6px}
.warn{background:#fff4c2;border:1px solid #d9b740;padding:6px 10px;margin:6px 0}
table.grid{border-collapse:collapse;margin:6px 0;width:100%}
table.grid th,table.grid td{border:1px solid #b9c0c8;padding:3px 6px;text-align:left}
table.grid th{background:#eef1f4}
tr.grp th{text-align:left;background:#eef1f4;padding:4px 6px}
.rate{margin-top:10px}
.prem{font-size:14px}
dialog{border:2px solid #3c4f66;padding:10px 14px;min-width:460px}
</style></head><body>{{BODY}}</body></html>"""

WORKSHEET_JS = r"""
<script>
const $ = (id) => document.getElementById(id);
let bikes = BIKES.slice();
function openPanel(key){
  document.querySelectorAll('.panel').forEach(p => {
    const on = p.dataset.panel === key;
    p.querySelector('.body').hidden = !on;
    p.querySelector('button.acc').setAttribute('aria-expanded', String(on));
  });
}
document.querySelectorAll('button.acc').forEach(b => b.onclick = () => {
  const key = b.closest('.panel').dataset.panel;
  if (b.getAttribute('aria-expanded') === 'true') { b.closest('.panel').querySelector('.body').hidden = true; b.setAttribute('aria-expanded','false'); }
  else openPanel(key);
});
const ORDER = ['basics','rider','garaging','motorcycles','coverage'];
const FIELDS = {basics:['effective_date','prior_insurance'], rider:['first_name','last_name','dob','phone','years_endorsed','safety_course'],
  garaging:['street','city','state','zip'], motorcycles:['annual_miles'], coverage:['bi_pd_limits','collision_deductible','comprehensive_deductible','cpe_coverage','roadside']};
async function savePanel(key){
  const p = document.querySelector(`.panel[data-panel="${key}"]`);
  p.querySelectorAll('[data-err]').forEach(e => e.textContent = '');
  p.querySelector('[data-panel-err]').textContent = '';
  const data = {};
  for (const f of FIELDS[key]) data[f] = $(f).value;
  if (key === 'motorcycles') data.motorcycles = bikes;
  const r = await fetch('/rater/api/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({panel:key, data})});
  if (r.status === 401) {
    $('banner').innerHTML = '<p class="warn" role="alert">Your session timed out. Redirecting to sign in…</p>';
    setTimeout(() => location.href = '/agent/login?expired=1&next=/rater/worksheet', 600);
    return;
  }
  const j = await r.json();
  if (!j.ok) {
    for (const [k,v] of Object.entries(j.errors||{})) { const e = p.querySelector(`[data-err="${k}"]`); if (e) e.textContent = v; else p.querySelector('[data-panel-err]').textContent = v; }
    return;
  }
  p.querySelector('[data-saved]').textContent = '✓ Saved';
  const i = ORDER.indexOf(key);
  if (i + 1 < ORDER.length) openPanel(ORDER[i+1]); else openPanel('');
}
document.querySelectorAll('[data-save]').forEach(b => b.onclick = () => savePanel(b.dataset.save));
function drawBikes(){
  const tb = document.querySelector('#bike-grid tbody'); tb.innerHTML = '';
  bikes.forEach((b, i) => {
    const tr = document.createElement('tr');
    for (const k of ['vin','year','make','model','engine_cc','ownership','lienholder']) { const td = document.createElement('td'); td.textContent = b[k] || '—'; tr.appendChild(td); }
    const td = document.createElement('td'); const x = document.createElement('button'); x.type='button'; x.textContent='Remove';
    x.onclick = () => { bikes.splice(i,1); drawBikes(); }; td.appendChild(x); tr.appendChild(td); tb.appendChild(tr);
  });
}
drawBikes();
let decoded = null;
window.applyDecoded = (c) => { decoded = c; $('m-year').value = c.year; $('m-make').value = c.make; $('m-model').value = c.model; $('m-cc').value = c.engine_cc; };
$('add-bike').onclick = () => { decoded = null; for (const id of ['m-vin','m-year','m-make','m-model','m-cc','m-lien']) $(id).value=''; $('m-own').value=''; $('lien-row').hidden = true; $('bike-err').textContent=''; $('bike').showModal(); };
$('m-vin').addEventListener('input', () => { decoded = null; for (const id of ['m-year','m-make','m-model','m-cc']) $(id).value=''; });
$('decode').onclick = () => { const v = $('m-vin').value.trim().toUpperCase(); if (v.length !== 17) { $('bike-err').textContent = 'Enter all 17 characters of the VIN.'; return; } $('bike-err').textContent=''; window.open('/rater/vin-decoder?vin=' + encodeURIComponent(v), 'vindecoder', 'width=560,height=360'); };
$('m-own').addEventListener('change', () => { $('lien-row').hidden = ($('m-own').value === 'Owned outright' || $('m-own').value === ''); });
$('bike-ok').onclick = () => {
  if (!decoded) { $('bike-err').textContent = 'Decode the VIN first.'; return; }
  const own = $('m-own').value; if (!own) { $('bike-err').textContent = 'Choose the ownership.'; return; }
  const lien = own === 'Owned outright' ? '' : $('m-lien').value.trim();
  if (own !== 'Owned outright' && !lien) { $('bike-err').textContent = 'Enter the lienholder or lessor.'; return; }
  bikes.push({vin: decoded.vin, year: decoded.year, make: decoded.make, model: decoded.model, engine_cc: decoded.engine_cc, ownership: own, lienholder: lien});
  drawBikes(); $('bike').close();
};
$('bike-cancel').onclick = () => $('bike').close();
// CPE worksheet
function cpeRows(){ return [...document.querySelectorAll('#cpe-grid tbody tr')].map(tr => parseInt((tr.querySelector('.v').value||'').replace(/[^0-9]/g,''),10) || 0); }
function cpeTotal(){ $('cpe-total').textContent = 'Worksheet total: $' + cpeRows().reduce((a,b)=>a+b,0).toLocaleString('en-US'); }
$('open-cpe').onclick = () => { document.querySelector('#cpe-grid tbody').innerHTML=''; cpeTotal(); $('cpe').showModal(); };
$('cpe-add').onclick = () => {
  const n = document.querySelectorAll('#cpe-grid tbody tr').length + 1;
  const tr = document.createElement('tr');
  tr.innerHTML = `<td><input class="i" aria-label="Item ${n}"></td><td><input class="v" aria-label="Value ${n}"></td>`;
  document.querySelector('#cpe-grid tbody').appendChild(tr);
  tr.querySelector('.v').addEventListener('input', cpeTotal);
};
$('cpe-apply').onclick = () => { const rows = cpeRows().slice(0, 3); $('cpe_coverage').value = String(rows.reduce((a,b)=>a+b,0)); $('cpe').close(); };
$('cpe-close').onclick = () => $('cpe').close();
openPanel('basics');
</script>"""
