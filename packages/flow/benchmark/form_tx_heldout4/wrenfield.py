"""Wrenfield Mutual: condo unit-owners (HO-6) quote, consumer single-page wizard.

One page renders four steps (unit, owner, coverage, review) with a stepper.
Each step saves through a JSON API; the review page reads the stored draft
back from the server. The association field is an async ARIA combobox. The
coverage step has an improvements estimator dialog. When the session ends,
an in-page sign-in dialog appears.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .common import (
    BasePortal,
    Request,
    Response,
    esc,
    money,
    parse_date,
    parse_int,
    slow,
)

RECORD_FIELDS = [
    "association", "street", "unit_number", "city", "state", "zip", "year_built", "floor",
    "occupancy", "seasonal_credit", "wood_stove",
    "first_name", "last_name", "dob", "email", "phone",
    "improvements", "personal_property", "loss_assessment", "water_backup", "deductible", "start_date",
]

ASSOCIATIONS = [
    "Larch Hollow Condominium Association (Tigard, OR)",
    "Larch Hollow Condominium Association (Beaverton, OR)",
    "Quarry Bend Lofts Owners Association (Portland, OR)",
    "Fennimore Court Condominiums (Salem, OR)",
    "Tidewater Row Condominium Association (Astoria, OR)",
    "Millrace Commons Owners Association (Eugene, OR)",
    "Juniper Ridge Terrace Condominiums (Bend, OR)",
    "Harrowgate Towers Condominium (Vancouver, WA)",
    "Harrowgate Park Condominium Owners (Vancouver, WA)",
    "Sablewood Flats Condominium Association (Spokane, WA)",
    "Orchard Lane Townhome Condominiums (Medford, OR)",
    "Kestrel Point Condominium Association (Bellingham, WA)",
    "Copperline Station Lofts (Boise, ID)",
]
STATES = ["OR", "WA", "ID", "CA", "NV"]
OCCUPANCY = ["Owner-occupied (primary residence)", "Secondary or seasonal home", "Rented to others"]
YES_NO = ["Yes", "No"]
LOSS_ASSESSMENT = ["$1,000", "$10,000", "$25,000", "$50,000"]
WATER_BACKUP = ["Not included", "$5,000", "$10,000", "$25,000"]
DEDUCTIBLES = [500, 1000, 2500]
FINISH = {"Builder grade": 45, "Upgraded": 70, "Custom luxury": 110}

STEPS = {
    1: ["association", "street", "unit_number", "city", "state", "zip", "year_built", "floor",
        "occupancy", "seasonal_credit", "wood_stove"],
    2: ["first_name", "last_name", "dob", "email", "phone"],
    3: ["improvements", "personal_property", "loss_assessment", "water_backup", "deductible", "start_date"],
}
LABELS = {
    "association": "Condominium association", "street": "Unit street address", "unit_number": "Unit number",
    "city": "City", "state": "State", "zip": "ZIP code", "year_built": "Year the building was built",
    "floor": "Floor the unit is on", "occupancy": "How is the unit occupied?",
    "seasonal_credit": "Seasonal occupancy credit", "wood_stove": "Wood-burning stove or fireplace insert",
    "first_name": "First name", "last_name": "Last name", "dob": "Date of birth", "email": "Email",
    "phone": "Mobile phone", "improvements": "Unit improvements and betterments (Coverage A)",
    "personal_property": "Personal property (Coverage C)", "loss_assessment": "Loss assessment",
    "water_backup": "Water and sewer backup", "deductible": "Deductible", "start_date": "Policy start date",
}
INT_FIELDS = {"year_built", "floor", "improvements", "personal_property", "deductible"}


def _us(iso: str) -> str:
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    return f"{m.group(2)}/{m.group(3)}/{m.group(1)}" if m else (iso or "")


class Wrenfield(BasePortal):
    portal_id = "wrenfield"
    cookie = "wf_sid"

    def fresh_draft(self) -> dict[str, Any]:
        d: dict[str, Any] = {f: "" for f in RECORD_FIELDS}
        d.update(water_backup="Not included", deductible=1000)
        d.update(self.config.get("prefill", {}))
        return d

    # ---- validation --------------------------------------------------
    def _clean(self, step: int, data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        out: dict[str, Any] = {}
        err: dict[str, str] = {}
        for f in STEPS[step]:
            v = str(data.get(f, "") if data.get(f) is not None else "").strip()
            out[f] = v
        if step == 1:
            if out["association"] not in ASSOCIATIONS:
                err["association"] = "Choose your association from the list."
            for f in ("street", "unit_number", "city"):
                if not out[f]:
                    err[f] = "Required."
            if out["state"] not in STATES:
                err["state"] = "Choose a state."
            if not re.fullmatch(r"\d{5}", out["zip"]):
                err["zip"] = "Enter a 5-digit ZIP code."
            yb = parse_int(out["year_built"])
            if yb is None or not 1850 <= yb <= 2026:
                err["year_built"] = "Enter a year between 1850 and 2026."
            fl = parse_int(out["floor"])
            if fl is None or not 1 <= fl <= 80:
                err["floor"] = "Enter a floor number."
            if out["occupancy"] not in OCCUPANCY:
                err["occupancy"] = "Choose how the unit is occupied."
            if out["occupancy"] == OCCUPANCY[1]:
                if out["seasonal_credit"] not in YES_NO:
                    err["seasonal_credit"] = "Answer this question."
            else:
                out["seasonal_credit"] = ""
            if out["wood_stove"] not in YES_NO:
                err["wood_stove"] = "Answer this question."
        elif step == 2:
            for f in ("first_name", "last_name"):
                if not out[f]:
                    err[f] = "Required."
            iso = parse_date(out["dob"])
            if not iso:
                err["dob"] = "Use MM/DD/YYYY."
            out["dob"] = iso or out["dob"]
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]+", out["email"], re.I):
                err["email"] = "Enter a valid email."
            if len(re.sub(r"\D", "", out["phone"])) != 10:
                err["phone"] = "Enter a 10-digit phone number."
        else:
            for f in ("improvements", "personal_property"):
                n = parse_int(out[f])
                if n is None or n <= 0:
                    err[f] = "Enter a whole-dollar amount."
            if out["loss_assessment"] not in LOSS_ASSESSMENT:
                err["loss_assessment"] = "Choose a limit."
            if out["water_backup"] not in WATER_BACKUP:
                err["water_backup"] = "Choose an option."
            if parse_int(out["deductible"]) not in DEDUCTIBLES:
                err["deductible"] = "Choose a deductible."
            iso = parse_date(out["start_date"])
            if not iso:
                err["start_date"] = "Use MM/DD/YYYY."
            out["start_date"] = iso or out["start_date"]
        for f in INT_FIELDS & set(out):
            n = parse_int(out[f])
            if n is not None:
                out[f] = n
        return out, err

    def _premium(self, d: dict[str, Any]) -> int:
        base = 180 + int(d.get("improvements") or 0) * 0.0021 + int(d.get("personal_property") or 0) * 0.0035
        base *= {500: 1.12, 1000: 1.0, 2500: 0.86}.get(int(d.get("deductible") or 1000), 1.0)
        if d.get("occupancy") == OCCUPANCY[2]:
            base *= 1.3
        if d.get("seasonal_credit") == "Yes":
            base *= 0.92
        if d.get("wood_stove") == "Yes":
            base += 45
        base += {"Not included": 0, "$5,000": 38, "$10,000": 61, "$25,000": 104}.get(d.get("water_backup"), 0)
        return int(round(base))

    def _display(self, f: str, v: Any) -> str:
        if v in ("", None):
            return "—"
        if f in ("improvements", "personal_property", "deductible"):
            return money(int(v))
        if f in ("dob", "start_date"):
            return _us(str(v))
        return str(v)

    # ---- routing -----------------------------------------------------
    def route(self, req: Request) -> Response:
        p = req.path
        if p == "/":
            return Response.redirect("/condo/quote")
        if p == "/account/login":
            if req.method == "POST":
                if self.check_login(req.form.get("username", ""), req.form.get("password", "")):
                    nxt = req.form.get("next") or "/condo/quote"
                    if not nxt.startswith("/"):
                        nxt = "/condo/quote"
                    return Response.redirect(nxt).set_cookie(self.cookie, self.new_session())
                return Response.html(self._login_page(req.form.get("next", ""), "Email or password is incorrect."), 401)
            return Response.html(self._login_page(req.query.get("next", ""), ""))
        if p == "/api/session" and req.method == "POST":
            body = req.json or {}
            if self.check_login(str(body.get("username", "")), str(body.get("password", ""))):
                return Response.json_({"ok": True}).set_cookie(self.cookie, self.new_session())
            return Response.json_({"ok": False, "error": "Email or password is incorrect."}, 401)
        if p == "/condo/quote":
            if not self.has_session(req):
                return Response.redirect("/account/login?next=/condo/quote")
            return Response.html(APP_HTML)
        m = re.fullmatch(r"/condo/quote/(WF-\d+)", p)
        if m:
            if not self.has_session(req):
                return Response.redirect("/account/login?next=" + p)
            return self._quote_page(m.group(1))
        if p.startswith("/api/condo/"):
            if not self.has_session(req):
                return Response.json_({"ok": False, "error": "session_expired"}, 401)
            return self._api(req, p[len("/api/condo/"):])
        return Response.html("<h1>Not found</h1>", 404)

    def _api(self, req: Request, what: str) -> Response:
        if what == "draft":
            with self.lock:
                d = {f: self.draft.get(f, "") for f in RECORD_FIELDS}
            for f in ("dob", "start_date"):
                d[f] = _us(str(d[f]))
            return Response.json_({"draft": d})
        if what.startswith("associations"):
            q = re.sub(r"\s+", " ", req.query.get("q", "")).strip().casefold()
            slow(0.35)
            hits = [a for a in ASSOCIATIONS if len(q) >= 3 and q in a.casefold()][:6]
            return Response.json_({"results": hits})
        if what == "step" and req.method in ("POST", "PUT"):
            body = req.json or {}
            step = int(body.get("step") or 0)
            if step not in STEPS:
                return Response.json_({"ok": False, "error": "bad step"}, 400)
            if step == 3 and self.fire_once("save:coverage"):
                with self.lock:
                    self.drop_sessions()
                    self.draft = self.fresh_draft()
                return Response.json_({"ok": False, "error": "session_expired"}, 401)
            clean, err = self._clean(step, body.get("data") or {})
            if err:
                return Response.json_({"ok": False, "errors": err}, 422)
            with self.lock:
                self.draft.update(clean)
                if step == 3 and self.config.get("promo_water_backup") and "promo" not in self.fired:
                    self.fired.add("promo")
                    self.draft["water_backup"] = self.config["promo_water_backup"]
            return Response.json_({"ok": True})
        if what == "review":
            with self.lock:
                d = dict(self.draft)
            rows = []
            for step in (1, 2, 3):
                for f in STEPS[step]:
                    if f == "seasonal_credit" and d.get("occupancy") != OCCUPANCY[1]:
                        continue
                    rows.append({"step": step, "label": LABELS[f], "value": self._display(f, d.get(f))})
            return Response.json_({"rows": rows, "premium": money(self._premium(d))})
        if what == "submit" and req.method == "POST":
            if not (req.json or {}).get("confirm"):
                return Response.json_({"ok": False, "error": "Confirm the information first."}, 422)
            with self.lock:
                d = dict(self.draft)
            rec: dict[str, Any] = {}
            for step in (1, 2, 3):
                clean, err = self._clean(step, {f: (_us(str(d[f])) if f in ("dob", "start_date") else d[f])
                                                 for f in STEPS[step]})
                if err:
                    return Response.json_({"ok": False, "error": f"Step {step} is incomplete.", "errors": err}, 422)
                rec.update(clean)
            with self.lock:
                self.quote_seq += 1
                ref = f"WF-{604200 + self.quote_seq}"
            rec["quote_ref"] = ref
            rec["premium"] = self._premium(rec)
            self.store_submission(rec)
            return Response.json_({"ok": True, "quote_ref": ref})
        return Response.json_({"error": "not found"}, 404)

    # ---- pages -------------------------------------------------------
    def _login_page(self, nxt: str, error: str) -> str:
        err = f'<p class="err" role="alert">{esc(error)}</p>' if error else ""
        return PAGE.replace("{{BODY}}", f"""
<main class="card narrow">
  <h1>Sign in to Wrenfield</h1>
  <p class="muted">Sign in to start or continue your condo quote.</p>
  {err}
  <form method="post" action="/account/login">
    <input type="hidden" name="next" value="{esc(nxt)}">
    <label for="u">Email</label><input id="u" name="username" type="email" autocomplete="username">
    <label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password">
    <button type="submit" class="primary">Sign in</button>
  </form>
</main>""")

    def _quote_page(self, ref: str) -> Response:
        with self.lock:
            rec = next((r for r in self.submissions if r.get("quote_ref") == ref), None)
        if rec is None:
            return Response.html("<h1>Quote not found</h1>", 404)
        rows = "".join(
            f"<tr><th scope='row'>{esc(LABELS[f])}</th><td>{esc(self._display(f, rec.get(f)))}</td></tr>"
            for f in RECORD_FIELDS if not (f == "seasonal_credit" and not rec.get(f))
        )
        return Response.html(PAGE.replace("{{BODY}}", f"""
<main class="card">
  <h1>Your condo quote is ready</h1>
  <p class="ref">Quote reference <strong id="quote-ref">{esc(ref)}</strong></p>
  <p class="premium">Estimated annual premium <strong id="premium">{esc(money(rec['premium']))}</strong></p>
  <table class="summary">{rows}</table>
  <p class="muted">A Wrenfield agent will contact you to finalize your policy.</p>
</main>"""))


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Wrenfield Mutual | Condo insurance quote</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#f3f6f4;color:#1d2b24}
header.top{background:#21493a;color:#fff;padding:14px 28px;font-weight:600;letter-spacing:.02em}
header.top span{font-weight:400;opacity:.8;margin-left:10px}
.card{background:#fff;max-width:880px;margin:28px auto;padding:28px 32px;border-radius:10px;box-shadow:0 1px 4px #0002}
.narrow{max-width:420px}
label{display:block;font-weight:600;margin:14px 0 4px}
input,select{font:inherit;padding:8px 10px;border:1px solid #9aa9a1;border-radius:6px;width:100%;box-sizing:border-box}
button{font:inherit;padding:9px 18px;border-radius:6px;border:1px solid #21493a;background:#fff;color:#21493a;cursor:pointer}
button.primary{background:#21493a;color:#fff;margin-top:18px}
.err{color:#a3261b;font-size:.9em;margin:4px 0}
.muted{color:#5d6b63}
.wizard{display:grid;grid-template-columns:210px 1fr;gap:28px;max-width:1040px;margin:28px auto;padding:0 18px}
ol.stepper{list-style:none;padding:0;margin:0}
ol.stepper li button{display:block;width:100%;text-align:left;border:none;background:none;padding:10px 12px;border-left:3px solid #c7d3cc;color:#5d6b63}
ol.stepper li button[aria-current=step]{border-left-color:#21493a;color:#21493a;font-weight:700;background:#e5eee9}
ol.stepper li button:disabled{opacity:.5;cursor:default}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:0 18px}
.combo{position:relative}
ul[role=listbox]{position:absolute;z-index:5;left:0;right:0;background:#fff;border:1px solid #9aa9a1;border-radius:0 0 6px 6px;margin:0;padding:0;list-style:none;max-height:240px;overflow:auto}
ul[role=listbox] li{padding:8px 10px;cursor:pointer}
ul[role=listbox] li:hover,ul[role=listbox] li[aria-selected=true]{background:#e5eee9}
fieldset{border:none;padding:0;margin:14px 0 0}
legend{font-weight:600;padding:0}
.radios label{display:inline-flex;align-items:center;gap:6px;font-weight:400;margin-right:22px}
.radios input{width:auto}
.inline{display:flex;gap:10px;align-items:end}
.inline input{flex:1}
table.summary{border-collapse:collapse;width:100%;margin-top:12px}
table.summary th,table.summary td{text-align:left;padding:7px 10px;border-bottom:1px solid #e1e8e4;vertical-align:top}
table.summary th{width:46%;font-weight:600;color:#3b4b42}
h3.sec{margin:22px 0 4px;display:flex;justify-content:space-between;align-items:center}
dialog{border:none;border-radius:10px;box-shadow:0 8px 30px #0004;padding:24px 28px;width:380px}
dialog::backdrop{background:#0006}
.estimate{font-size:1.1em;margin:16px 0 4px}
.ref,.premium{font-size:1.1em}
.banner{background:#fdf0d5;border:1px solid #e6c06a;padding:10px 14px;border-radius:6px;margin-bottom:12px}
</style></head><body>
<header class="top">Wrenfield Mutual <span>Condo unit-owners insurance</span></header>
{{BODY}}
</body></html>"""


def _select(fid: str, options: list[str], placeholder: bool = True) -> str:
    opts = ('<option value="">Select…</option>' if placeholder else "") + "".join(
        f'<option value="{esc(o)}">{esc(o)}</option>' for o in options
    )
    return f'<label for="{fid}">{esc(LABELS[fid])}</label><select id="{fid}" name="{fid}">{opts}</select><p class="err" data-err="{fid}"></p>'


def _text(fid: str, placeholder: str = "", kind: str = "text") -> str:
    ph = f' placeholder="{esc(placeholder)}"' if placeholder else ""
    return f'<label for="{fid}">{esc(LABELS[fid])}</label><input id="{fid}" name="{fid}" type="{kind}"{ph}><p class="err" data-err="{fid}"></p>'


_DEDUCT = "".join(
    f'<label><input type="radio" name="deductible" value="{d}"> {esc(money(d))}</label>' for d in DEDUCTIBLES
)

APP_BODY = f"""
<div class="wizard">
<nav aria-label="Quote steps"><ol class="stepper">
  <li><button type="button" data-go="1">1. Your unit</button></li>
  <li><button type="button" data-go="2">2. About you</button></li>
  <li><button type="button" data-go="3">3. Coverage</button></li>
  <li><button type="button" data-go="4">4. Review</button></li>
</ol></nav>
<main class="card" style="margin:0;max-width:none">
<div id="banner"></div>
<section data-step="1" hidden>
  <h2>Your unit</h2>
  <div class="combo">
    <label for="association">{LABELS['association']}</label>
    <input id="association" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="assoc-list" autocomplete="off" placeholder="Start typing the association name">
    <ul id="assoc-list" role="listbox" aria-label="Associations" hidden></ul>
    <p class="err" data-err="association"></p>
  </div>
  {_text('street')}
  <div class="row2"><div>{_text('unit_number')}</div><div>{_text('city')}</div></div>
  <div class="row2"><div>{_select('state', STATES)}</div><div>{_text('zip')}</div></div>
  <div class="row2"><div>{_text('year_built')}</div><div>{_text('floor')}</div></div>
  {_select('occupancy', OCCUPANCY)}
  <div id="seasonal-wrap" hidden>{_select('seasonal_credit', YES_NO)}
    <p class="muted">Credit for units left unoccupied three or more consecutive months each year.</p></div>
  {_select('wood_stove', YES_NO)}
  <button type="button" class="primary" data-save="1">Continue</button>
</section>
<section data-step="2" hidden>
  <h2>About you</h2>
  <div class="row2"><div>{_text('first_name')}</div><div>{_text('last_name')}</div></div>
  {_text('dob', 'MM/DD/YYYY')}
  {_text('email', '', 'email')}
  {_text('phone')}
  <button type="button" class="primary" data-save="2">Continue</button>
</section>
<section data-step="3" hidden>
  <h2>Coverage</h2>
  <label for="improvements">{LABELS['improvements']}</label>
  <div class="inline"><input id="improvements" name="improvements" inputmode="numeric">
    <button type="button" id="open-est">Estimate it</button></div>
  <p class="err" data-err="improvements"></p>
  {_text('personal_property')}
  <div class="row2"><div>{_select('loss_assessment', LOSS_ASSESSMENT)}</div>
    <div>{_select('water_backup', WATER_BACKUP, placeholder=False)}</div></div>
  <fieldset class="radios"><legend>Deductible</legend>{_DEDUCT}<p class="err" data-err="deductible"></p></fieldset>
  {_text('start_date', 'MM/DD/YYYY')}
  <button type="button" class="primary" data-save="3">Continue to review</button>
</section>
<section data-step="4" hidden>
  <h2>Review your quote</h2>
  <p class="muted">Check everything below before you get your quote.</p>
  <div id="review"></div>
  <p class="premium">Estimated annual premium <strong id="review-premium"></strong></p>
  <label style="font-weight:400;display:flex;gap:8px;align-items:center"><input type="checkbox" id="confirm" style="width:auto"> I confirm the information above is accurate</label>
  <p class="err" id="submit-err"></p>
  <button type="button" class="primary" id="get-quote">Get my quote</button>
</section>
</main>
</div>
<dialog id="est" aria-labelledby="est-h">
  <h2 id="est-h">Improvements estimator</h2>
  <p class="muted">Estimate the value of the walls-in finishes you own.</p>
  <label for="est-sqft">Square footage</label><input id="est-sqft" inputmode="numeric">
  <label for="est-finish">Finish level</label>
  <select id="est-finish">{''.join(f'<option>{k}</option>' for k in FINISH)}</select>
  <p class="estimate" id="est-out">Estimated improvements value: —</p>
  <button type="button" class="primary" id="est-use">Use this amount</button>
  <button type="button" id="est-cancel">Cancel</button>
</dialog>
<dialog id="relogin" aria-labelledby="rl-h">
  <h2 id="rl-h">Your session has ended</h2>
  <p class="muted">For your security we signed you out. Sign in again to continue.</p>
  <label for="rl-u">Email</label><input id="rl-u" type="email">
  <label for="rl-p">Password</label><input id="rl-p" type="password">
  <p class="err" id="rl-err"></p>
  <button type="button" class="primary" id="rl-go">Sign in</button>
</dialog>
"""

APP_JS = r"""
<script>
const RATES = __RATES__;
const STEPS = __STEPS__;
let current = 1, reached = 1, chosenAssoc = '';
const $ = (id) => document.getElementById(id);
function showStep(n){
  current = n; if (n > reached) reached = n;
  document.querySelectorAll('section[data-step]').forEach(s => s.hidden = (+s.dataset.step !== n));
  document.querySelectorAll('[data-go]').forEach(b => {
    const k = +b.dataset.go; b.disabled = k > reached;
    if (k === n) b.setAttribute('aria-current','step'); else b.removeAttribute('aria-current');
  });
  if (n === 4) loadReview();
}
function syncSeasonal(){ $('seasonal-wrap').hidden = $('occupancy').value !== 'Secondary or seasonal home'; }
async function loadDraft(){
  const r = await fetch('/api/condo/draft');
  if (r.status === 401) return askLogin();
  const d = (await r.json()).draft;
  for (const step of Object.values(STEPS)) for (const f of step) {
    if (f === 'deductible') { document.querySelectorAll('input[name=deductible]').forEach(x => x.checked = String(x.value) === String(d[f])); continue; }
    const el = $(f); if (el) el.value = d[f] === null ? '' : d[f];
  }
  chosenAssoc = d.association || '';
  syncSeasonal();
}
function collect(step){
  const out = {};
  for (const f of STEPS[step]) {
    if (f === 'deductible') { const c = document.querySelector('input[name=deductible]:checked'); out[f] = c ? c.value : ''; continue; }
    if (f === 'association') { out[f] = chosenAssoc; continue; }
    out[f] = $(f).value;
  }
  return out;
}
async function save(step){
  document.querySelectorAll('[data-err]').forEach(e => e.textContent = '');
  const r = await fetch('/api/condo/step', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({step, data: collect(step)})});
  if (r.status === 401) return askLogin();
  const j = await r.json();
  if (!j.ok) { for (const [k,v] of Object.entries(j.errors||{})) { const e = document.querySelector(`[data-err="${k}"]`); if (e) e.textContent = v; } return; }
  showStep(step + 1);
}
function askLogin(){ $('rl-err').textContent=''; $('rl-p').value=''; if (!$('relogin').open) $('relogin').showModal(); }
$('rl-go').onclick = async () => {
  const r = await fetch('/api/session', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({username: $('rl-u').value, password: $('rl-p').value})});
  if (!r.ok) { $('rl-err').textContent = 'Email or password is incorrect.'; return; }
  $('relogin').close();
  $('banner').innerHTML = '<p class="banner" role="status">You are signed in again. Please check your answers.</p>';
  await loadDraft();
};
document.querySelectorAll('[data-save]').forEach(b => b.onclick = () => save(+b.dataset.save));
document.querySelectorAll('[data-go]').forEach(b => b.onclick = () => showStep(+b.dataset.go));
$('occupancy').addEventListener('change', syncSeasonal);
// association combobox
let timer = null, seq = 0;
const box = $('association'), list = $('assoc-list');
box.addEventListener('input', () => {
  chosenAssoc = '';
  clearTimeout(timer);
  timer = setTimeout(async () => {
    const my = ++seq;
    const r = await fetch('/api/condo/associations?q=' + encodeURIComponent(box.value));
    if (r.status === 401) return askLogin();
    const j = await r.json();
    if (my !== seq) return;
    list.innerHTML = '';
    j.results.forEach((name, i) => {
      const li = document.createElement('li'); li.setAttribute('role','option'); li.id = 'assoc-opt-' + i; li.textContent = name;
      li.onmousedown = (ev) => { ev.preventDefault(); box.value = name; chosenAssoc = name; list.hidden = true; box.setAttribute('aria-expanded','false'); };
      list.appendChild(li);
    });
    list.hidden = j.results.length === 0; box.setAttribute('aria-expanded', String(j.results.length > 0));
  }, 200);
});
box.addEventListener('blur', () => setTimeout(() => { list.hidden = true; box.setAttribute('aria-expanded','false'); }, 150));
// estimator
let applied = null;
function estimate(){ const s = parseInt(($('est-sqft').value||'').replace(/[^0-9]/g,''),10); return isNaN(s) ? null : s * RATES[$('est-finish').value]; }
function showEst(){ const v = estimate(); $('est-out').textContent = 'Estimated improvements value: ' + (v === null ? '—' : '$' + v.toLocaleString('en-US')); }
$('est-sqft').addEventListener('input', () => { applied = estimate(); showEst(); });
$('est-finish').addEventListener('change', showEst);
$('open-est').onclick = () => { $('est-sqft').value=''; $('est-finish').selectedIndex = 0; applied = null; showEst(); $('est').showModal(); };
$('est-use').onclick = () => { if (applied !== null) $('improvements').value = String(applied); $('est').close(); };
$('est-cancel').onclick = () => $('est').close();
// review
async function loadReview(){
  $('review').innerHTML = ''; $('review-premium').textContent = '';
  const r = await fetch('/api/condo/review');
  if (r.status === 401) return askLogin();
  const j = await r.json();
  const names = {1:'Your unit', 2:'About you', 3:'Coverage'};
  let h = '';
  for (const s of [1,2,3]) {
    h += `<h3 class="sec">${names[s]} <button type="button" data-edit="${s}">Edit</button></h3><table class="summary">`;
    for (const row of j.rows.filter(x => x.step === s)) h += `<tr><th scope="row">${row.label}</th><td>${row.value}</td></tr>`;
    h += '</table>';
  }
  $('review').innerHTML = h;
  $('review-premium').textContent = j.premium;
  document.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => showStep(+b.dataset.edit));
}
$('get-quote').onclick = async () => {
  $('submit-err').textContent = '';
  const r = await fetch('/api/condo/submit', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({confirm: $('confirm').checked})});
  if (r.status === 401) return askLogin();
  const j = await r.json();
  if (!j.ok) { $('submit-err').textContent = j.error; return; }
  location.href = '/condo/quote/' + j.quote_ref;
};
loadDraft().then(() => showStep(1));
</script>
""".replace("__RATES__", json.dumps(FINISH)).replace("__STEPS__", json.dumps({str(k): v for k, v in STEPS.items()}))

APP_HTML = PAGE.replace("{{BODY}}", APP_BODY + APP_JS)
