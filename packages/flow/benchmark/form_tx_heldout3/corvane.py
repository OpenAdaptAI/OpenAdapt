"""Corvane Mutual: homeowners (HO-3) quote wizard for appointed agents.

A single-page wizard rendered in the browser from a JSON step schema, with a
left-hand step list. Each step saves through ``PUT /api/quote/step/<n>``.
Applicant details can come prefilled from an existing Corvane customer
record, and dwelling details are filled in from public property records once
the address is saved. Lender selection is a type-ahead over a lender
directory; the Coverage A limit has a replacement-cost estimator dialog. An
expired session sends the agent back to the sign-in page, and the quote is
restored from its last saved version. The review step lists every stored
answer; ``Request quote`` stores the submission and shows the quote.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote as urlquote

from .case_defs import (
    CV_CONSTRUCTION,
    CV_FIELDS,
    CV_LENDERS,
    CV_OCCUPANCY,
    CV_ROOF,
    CV_STATES,
)
from .common import BasePortal, Request, Response, esc, money, parse_date, parse_int
from .estimators import replacement_cost

RECORD_FIELDS = list(CV_FIELDS)

LIABILITY = [100000, 300000, 500000]
DEDUCTIBLES = [1000, 2500, 5000]


def _opts(values: list[Any], fmt=str) -> list[list[str]]:
    return [[str(v), fmt(v)] for v in values]


STEPS: list[dict[str, Any]] = [
    {
        "n": 1, "title": "Applicant and property",
        "fields": [
            {"name": "first_name", "label": "First name", "kind": "text"},
            {"name": "last_name", "label": "Last name", "kind": "text"},
            {"name": "date_of_birth", "label": "Date of birth", "kind": "date"},
            {"name": "email", "label": "Email", "kind": "text"},
            {"name": "phone", "label": "Phone", "kind": "text"},
            {"name": "co_applicant_name", "label": "Co-applicant full name", "kind": "text", "optional": True,
             "help": "Spouse or other person on the deed. Leave blank if none."},
            {"name": "property_street", "label": "Property street address", "kind": "text"},
            {"name": "property_city", "label": "City", "kind": "text"},
            {"name": "property_state", "label": "State", "kind": "select", "options": _opts(CV_STATES)},
            {"name": "property_zip", "label": "ZIP code", "kind": "text"},
        ],
    },
    {
        "n": 2, "title": "Dwelling",
        "fields": [
            {"name": "year_built", "label": "Year built", "kind": "int"},
            {"name": "square_feet", "label": "Finished living area (sq ft)", "kind": "int"},
            {"name": "construction", "label": "Construction type", "kind": "select",
             "options": _opts(CV_CONSTRUCTION)},
            {"name": "roof_material", "label": "Roof material", "kind": "select", "options": _opts(CV_ROOF)},
            {"name": "roof_year", "label": "Year roof was installed or last replaced", "kind": "int"},
        ],
    },
    {
        "n": 3, "title": "Household",
        "fields": [
            {"name": "occupancy", "label": "Occupancy", "kind": "radio", "options": _opts(CV_OCCUPANCY)},
            {"name": "dogs", "label": "Are any dogs kept at this address?", "kind": "radio",
             "options": [["Yes", "Yes"], ["No", "No"]]},
            {"name": "dog_breeds", "label": "Breed of each dog", "kind": "text", "optional": True,
             "show_if": ["dogs", "Yes"], "help": "List every dog, for example: Beagle, Boxer mix."},
            {"name": "dog_bite_history",
             "label": "Has any dog at this address ever bitten a person or another animal?",
             "kind": "radio", "options": [["Yes", "Yes"], ["No", "No"]], "show_if": ["dogs", "Yes"]},
        ],
    },
    {
        "n": 4, "title": "Lender and coverage",
        "fields": [
            {"name": "mortgagee", "label": "Mortgagee (lender)", "kind": "typeahead",
             "help": "Type the lender's name and pick its mortgagee clause from the directory."},
            {"name": "loan_number", "label": "Loan number", "kind": "text"},
            {"name": "dwelling_limit", "label": "Dwelling (Coverage A) limit", "kind": "money",
             "estimator": True},
            {"name": "personal_liability", "label": "Personal liability", "kind": "select",
             "options": _opts(LIABILITY, money)},
            {"name": "deductible", "label": "All-perils deductible", "kind": "select",
             "options": _opts(DEDUCTIBLES, money)},
            {"name": "effective_date", "label": "Policy effective date", "kind": "date"},
        ],
    },
]
FIELD_STEP = {f["name"]: s["n"] for s in STEPS for f in s["fields"]}
FIELD_DEF = {f["name"]: f for s in STEPS for f in s["fields"]}
STEP_FIELDS = {s["n"]: [f["name"] for f in s["fields"]] for s in STEPS}

DEFAULTS: dict[str, Any] = {name: "" for name in RECORD_FIELDS}
DEFAULTS.update({"occupancy": CV_OCCUPANCY[0], "dogs": "No", "personal_liability": "300000",
                 "deductible": "1000"})


def _display(name: str, value: Any) -> str:
    if value in ("", None):
        return "—"
    kind = FIELD_DEF[name]["kind"]
    if kind == "date":
        iso = parse_date(str(value))
        return f"{iso[5:7]}/{iso[8:10]}/{iso[:4]}" if iso else str(value)
    if kind == "money" or name in ("personal_liability", "deductible"):
        n = parse_int(value)
        return money(n) if n is not None else str(value)
    return str(value)


CSS = """
:root{--ink:#1b2a33;--muted:#5d6b73;--line:#d5dde1;--brand:#0f5e63;--bg:#f3f6f7;--warn:#fff6d8;--err:#a3261b}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,Segoe UI,Arial,sans-serif;color:var(--ink);background:var(--bg)}
header{background:var(--brand);color:#fff;padding:10px 20px;display:flex;justify-content:space-between;align-items:center}
header b{font-size:18px;letter-spacing:.02em}
.wrap{display:flex;max-width:1080px;margin:18px auto;gap:18px;padding:0 12px}
nav.steps{width:220px;flex:none}nav.steps ol{list-style:none;margin:0;padding:0}
nav.steps li{padding:9px 12px;border-left:4px solid var(--line);color:var(--muted)}
nav.steps li.cur{border-color:var(--brand);color:var(--ink);font-weight:600;background:#fff}
nav.steps li.done{color:var(--ink)}
main{flex:1;background:#fff;border:1px solid var(--line);border-radius:6px;padding:20px 24px}
h1{font-size:20px;margin:0 0 14px}.fld{margin:0 0 14px}.fld>label,.fld>.lbl{display:block;font-weight:600;margin-bottom:4px}
.fld input[type=text]{width:100%;max-width:440px;padding:7px 9px;border:1px solid #9fb0b8;border-radius:4px;font:inherit}
.fld select{padding:6px 8px;font:inherit;min-width:220px}.help{color:var(--muted);font-size:13px;margin-top:3px}
.radio{display:block;margin:3px 0}.err{color:var(--err);font-size:13px}.banner{background:var(--warn);border:1px solid #e9d68c;padding:9px 12px;border-radius:4px;margin-bottom:14px}
button{font:inherit;padding:8px 16px;border-radius:4px;border:1px solid var(--brand);background:var(--brand);color:#fff;cursor:pointer}
button.sec{background:#fff;color:var(--brand)}.row{display:flex;gap:8px;align-items:center}
.ta{position:relative;max-width:560px}.ta ul{position:absolute;z-index:5;left:0;right:0;top:100%;margin:2px 0 0;padding:0;list-style:none;background:#fff;border:1px solid #9fb0b8;border-radius:4px;max-height:220px;overflow:auto}
.ta li{padding:7px 9px;cursor:pointer;font-size:14px}.ta li:hover,.ta li[aria-selected=true]{background:#e4f0f1}
table.sum{border-collapse:collapse;width:100%}table.sum th,table.sum td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
table.sum th{width:45%;font-weight:600}dialog{border:1px solid var(--line);border-radius:6px;padding:18px 22px;max-width:460px}
dialog table td{padding:4px 10px 4px 0}.tot td{font-weight:700;border-top:1px solid var(--line)}
"""

APP_JS = r"""
const S = JSON.parse(document.getElementById('schema').textContent);
let state = null, cur = 1, busy = false;
const $ = (s, r=document) => r.querySelector(s);
const h = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function stepFromUrl(){ const p = new URLSearchParams(location.search).get('step'); return p ? parseInt(p,10) : 0; }
async function api(method, url, body){
  const r = await fetch(url, {method, headers:{'Content-Type':'application/json'}, body: body ? JSON.stringify(body) : undefined});
  if (r.status === 401){ location.href = '/login?expired=1&next=' + encodeURIComponent('/quote?step=' + cur); throw new Error('expired'); }
  return r.json();
}
async function load(){ state = await api('GET', '/api/quote'); cur = stepFromUrl() || state.resume_step || 1; render(); }
function nav(){
  const items = S.steps.map(s => `<li class="${s.n===cur?'cur':(state.saved.includes(s.n)?'done':'')}" data-step="${s.n}">${s.n}. ${h(s.title)}</li>`);
  items.push(`<li class="${cur===5?'cur':''}" data-step="5">5. Review</li>`);
  $('#steps').innerHTML = '<ol>' + items.join('') + '</ol>';
}
function fieldHtml(f, v){
  const id = 'f-' + f.name; let ctl = '';
  if (f.kind === 'select'){
    ctl = `<select id="${id}" name="${f.name}"><option value="">Select…</option>` +
      f.options.map(o => `<option value="${h(o[0])}"${String(v)===o[0]?' selected':''}>${h(o[1])}</option>`).join('') + '</select>';
    return `<label for="${id}">${h(f.label)}</label>${ctl}`;
  }
  if (f.kind === 'radio'){
    ctl = f.options.map((o,i) => `<label class="radio"><input type="radio" name="${f.name}" id="${id}-${i}" value="${h(o[0])}"${String(v)===o[0]?' checked':''}> ${h(o[1])}</label>`).join('');
    return `<div class="lbl" id="${id}-lbl">${h(f.label)}</div><div role="radiogroup" aria-labelledby="${id}-lbl">${ctl}</div>`;
  }
  if (f.kind === 'typeahead'){
    return `<label for="${id}">${h(f.label)}</label><div class="ta"><input type="text" id="${id}" name="${f.name}" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="${id}-list" autocomplete="off" value="${h(v)}"><ul id="${id}-list" role="listbox" hidden></ul></div>`;
  }
  const ph = f.kind === 'date' ? ' placeholder="MM/DD/YYYY"' : '';
  ctl = `<input type="text" id="${id}" name="${f.name}" value="${h(v)}"${ph}>`;
  if (f.estimator) ctl = `<div class="row">${ctl}<button type="button" class="sec" id="est-open">Estimate replacement cost</button></div>`;
  return `<label for="${id}">${h(f.label)}</label>${ctl}`;
}
function render(){
  nav(); history.replaceState(null, '', '/quote?step=' + cur);
  const banners = (state.banners[cur] || []).concat(state.banners.all || []);
  let out = banners.map(b => `<div class="banner" role="status">${h(b)}</div>`).join('');
  if (cur === 5) return renderReview(out);
  const st = S.steps[cur-1];
  out += `<h1>${h(st.title)}</h1><form id="stepform" novalidate>`;
  for (const f of st.fields){
    const v = state.draft[f.name] ?? '';
    out += `<div class="fld" data-name="${f.name}" data-kind="${f.kind}"${f.show_if?` data-show-if="${f.show_if[0]}=${f.show_if[1]}"`:''}>` +
      fieldHtml(f, v) + (f.help ? `<div class="help">${h(f.help)}</div>` : '') + `<div class="err" data-err="${f.name}"></div></div>`;
  }
  out += `<div class="row">${cur>1?'<button type="button" class="sec" id="back">Back</button>':''}<button type="submit" id="save">Save and continue</button></div></form>`;
  $('#app').innerHTML = out; $('#app').dataset.step = cur; $('#app').dataset.ready = String(Date.now());
  wire();
}
function applyShowIf(){
  document.querySelectorAll('[data-show-if]').forEach(el => {
    const [k, want] = el.dataset.showIf.split('=');
    const sel = document.querySelector(`input[name="${k}"]:checked`);
    el.hidden = !(sel && sel.value === want);
  });
}
function wire(){
  applyShowIf();
  document.querySelectorAll('input[type=radio]').forEach(r => r.addEventListener('change', applyShowIf));
  $('#stepform').addEventListener('submit', e => { e.preventDefault(); save(); });
  if ($('#back')) $('#back').onclick = () => { cur -= 1; render(); };
  const ta = $('input[role=combobox]'); if (ta) wireTypeahead(ta);
  if ($('#est-open')) $('#est-open').onclick = openEstimator;
}
function wireTypeahead(inp){
  const list = document.getElementById(inp.id + '-list'); let timer = null;
  inp.addEventListener('input', () => {
    clearTimeout(timer); inp.dataset.picked = '';
    timer = setTimeout(async () => {
      const q = inp.value.trim(); if (q.length < 3){ list.hidden = true; return; }
      const res = await api('GET', '/api/lenders?q=' + encodeURIComponent(q));
      list.innerHTML = res.matches.map((m,i) => `<li role="option" id="${inp.id}-opt-${i}" aria-selected="false">${h(m)}</li>`).join('') ||
        '<li role="presentation" class="none">No lenders found</li>';
      list.hidden = false; inp.setAttribute('aria-expanded', 'true');
      list.querySelectorAll('[role=option]').forEach(li => li.addEventListener('click', () => {
        inp.value = li.textContent; inp.dataset.picked = '1'; list.hidden = true; inp.setAttribute('aria-expanded','false');
      }));
    }, 250);
  });
}
async function openEstimator(){
  const est = await api('GET', '/api/estimate');
  const d = $('#estdlg');
  if (est.error){ d.innerHTML = `<p>${h(est.error)}</p><button type="button" id="est-close">Close</button>`; }
  else {
    d.innerHTML = `<h2 style="margin-top:0;font-size:17px">Replacement cost estimate</h2>
      <p class="help">Based on ${h(est.square_feet)} sq ft, ${h(est.construction)} construction (from the Dwelling step).</p>
      <table><tr><td>Base rebuild cost</td><td>${h(est.base_display)}</td></tr>
      <tr><td>Code and debris allowance (8%)</td><td>${h(est.allowance_display)}</td></tr>
      <tr class="tot"><td>Estimated replacement cost</td><td id="est-total">${h(est.total_display)}</td></tr></table>
      <div class="row" style="margin-top:14px"><button type="button" id="est-use">Use this estimate</button><button type="button" class="sec" id="est-close">Cancel</button></div>`;
    $('#est-use').onclick = () => { $('#f-dwelling_limit').value = est.apply_display; d.close(); };
  }
  $('#est-close').onclick = () => d.close();
  d.showModal();
}
function collect(){
  const vals = {};
  for (const f of S.steps[cur-1].fields){
    const box = document.querySelector(`.fld[data-name="${f.name}"]`);
    if (box.hidden){ vals[f.name] = ''; continue; }
    if (f.kind === 'radio'){ const c = document.querySelector(`input[name="${f.name}"]:checked`); vals[f.name] = c ? c.value : ''; }
    else vals[f.name] = document.getElementById('f-' + f.name).value;
  }
  return vals;
}
async function save(){
  if (busy) return; busy = true;
  try {
    document.querySelectorAll('[data-err]').forEach(e => e.textContent = '');
    const res = await api('PUT', '/api/quote/step/' + cur, {values: collect()});
    if (res.errors){ for (const [k,m] of Object.entries(res.errors)){ const e = document.querySelector(`[data-err="${k}"]`); if (e) e.textContent = m; } $('#app').dataset.errors = '1'; return; }
    state = res.state; cur = res.next_step; render();
  } finally { busy = false; }
}
function renderReview(out){
  out += '<h1>Review</h1><p class="help">Check every answer before you request the quote.</p>';
  for (const st of S.steps){
    out += `<h2 style="font-size:16px;margin:16px 0 6px">${h(st.title)} <button type="button" class="sec" data-edit="${st.n}" style="padding:2px 10px;font-size:13px">Edit</button></h2><table class="sum">`;
    for (const f of st.fields){
      if (f.show_if && state.draft[f.show_if[0]] !== f.show_if[1]) continue;
      out += `<tr data-name="${f.name}"><th>${h(f.label)}</th><td>${h(state.display[f.name])}</td></tr>`;
    }
    out += '</table>';
  }
  out += '<div class="row" style="margin-top:18px"><button type="button" id="submit">Request quote</button></div><div class="err" id="submit-err"></div>';
  $('#app').innerHTML = out; $('#app').dataset.step = 5; $('#app').dataset.ready = String(Date.now());
  document.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => { cur = parseInt(b.dataset.edit,10); render(); });
  $('#submit').onclick = async () => {
    if (busy) return; busy = true;
    try { const res = await api('POST', '/api/quote/submit', {});
      if (res.error){ $('#submit-err').textContent = res.error; return; }
      location.href = '/quote/result/' + res.quote_ref;
    } finally { busy = false; }
  };
}
load();
"""


def page(title: str, body: str, user: str = "") -> str:
    who = f"<span>Agency {esc(user)}</span>" if user else ""
    return (
        f"<!doctype html><html lang=en><head><meta charset=utf-8><title>{esc(title)}</title>"
        f"<style>{CSS}</style></head><body><header><b>Corvane Mutual</b><span>AgentLink · Homeowners</span>"
        f"{who}</header>{body}</body></html>"
    )


class Corvane(BasePortal):
    portal_id = "corvane"
    cookie = "cvsid"

    def fresh_draft(self) -> dict[str, Any]:
        d: dict[str, Any] = dict(DEFAULTS)
        cfg = getattr(self, "config", {}) or {}
        prior = cfg.get("prior_record")
        if prior:
            for k, v in prior.items():
                if k in d:
                    d[k] = v
        return d

    def reset(self) -> None:
        super().reset()
        with self.lock:
            self.saved: list[int] = []
            self.records_applied = False
            self.restored = False
            self.quotes: dict[str, dict[str, Any]] = {}

    def load_case(self, case_id: str) -> bool:
        ok = super().load_case(case_id)
        if ok:
            with self.lock:
                self.saved = []
                self.records_applied = False
                self.restored = False
        return ok

    # ---- helpers ----------------------------------------------------
    def _banners(self) -> dict[Any, list[str]]:
        out: dict[Any, list[str]] = {}
        prior = self.config.get("prior_record")
        if prior:
            out[1] = [f"Some answers were filled in from an existing Corvane record ({prior['source']}). "
                      "Review them before you continue."]
        if self.records_applied:
            out[2] = ["Dwelling details were filled in from public property records."]
        if self.restored:
            out["all"] = ["Your session ended. We restored this quote from its last saved version."]
        return out

    def _state(self) -> dict[str, Any]:
        d = self.draft
        resume = next((n for n in (1, 2, 3, 4) if n not in self.saved), 5)
        return {
            "draft": {k: (_display(k, d.get(k)) if FIELD_DEF[k]["kind"] == "date" and d.get(k) else d.get(k, ""))
                      for k in RECORD_FIELDS},
            "display": {k: _display(k, d.get(k, "")) for k in RECORD_FIELDS},
            "saved": sorted(self.saved),
            "banners": self._banners(),
            "resume_step": resume,
        }

    def _apply_public_record(self) -> None:
        pr = self.config.get("public_record") or {}
        for k, v in pr.items():
            self.draft[k] = str(v)
        self.records_applied = True

    def _validate(self, n: int, vals: dict[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
        clean: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for name in STEP_FIELDS[n]:
            f = FIELD_DEF[name]
            raw = str(vals.get(name, "") or "").strip()
            if f.get("show_if"):
                dep = str(vals.get(f["show_if"][0], ""))
                if dep != f["show_if"][1]:
                    clean[name] = ""
                    continue
            if not raw:
                if not f.get("optional"):
                    errors[name] = f"{f['label']} is required."
                clean[name] = ""
                continue
            kind = f["kind"]
            if kind == "date":
                iso = parse_date(raw)
                if not iso:
                    errors[name] = "Enter the date as MM/DD/YYYY."
                clean[name] = iso or raw
            elif kind in ("int", "money"):
                v = parse_int(raw)
                if v is None or v < 0:
                    errors[name] = "Enter a whole number."
                clean[name] = str(v) if v is not None else raw
            elif kind in ("select", "radio"):
                if raw not in [o[0] for o in f["options"]]:
                    errors[name] = "Choose one of the options."
                clean[name] = raw
            elif kind == "typeahead":
                if raw not in CV_LENDERS:
                    errors[name] = "Pick the lender's mortgagee clause from the directory."
                clean[name] = raw
            else:
                clean[name] = raw
        if n == 2 and not errors:
            yb, ry = parse_int(clean["year_built"]), parse_int(clean["roof_year"])
            if yb and ry and ry < yb:
                errors["roof_year"] = "The roof cannot be older than the house."
        return clean, errors

    def _revert_after_expiry(self) -> None:
        """Session ended: restore the version saved when the quote was created (after step 1)."""
        for n in (2, 3, 4):
            for name in STEP_FIELDS[n]:
                self.draft[name] = DEFAULTS[name]
        self._apply_public_record()
        self.saved = [1, 2, 3]
        self.restored = True

    def _record(self) -> dict[str, Any]:
        d = self.draft
        rec: dict[str, Any] = {}
        for k in RECORD_FIELDS:
            v = d.get(k, "")
            kind = FIELD_DEF[k]["kind"]
            if k == "dogs":
                rec[k] = v == "Yes"
            elif kind in ("int", "money") or k in ("personal_liability", "deductible"):
                rec[k] = parse_int(v)
            else:
                rec[k] = v
        return rec

    def _premium(self, rec: dict[str, Any]) -> int:
        base = (rec["dwelling_limit"] or 0) * 0.0031
        base *= {1000: 1.0, 2500: 0.9, 5000: 0.82}.get(rec["deductible"] or 1000, 1.0)
        base += {100000: 0, 300000: 28, 500000: 52}.get(rec["personal_liability"] or 0, 0)
        if rec["dogs"]:
            base += 35
        return int(round(base))

    # ---- routes -----------------------------------------------------
    def route(self, req: Request) -> Response:
        p = req.path
        if p in ("/", "/quote/start"):
            return Response.redirect("/quote")
        if p == "/login":
            return self.login(req)
        if p == "/logout":
            return Response.redirect("/login").clear_cookie(self.cookie)
        if p.startswith("/api/"):
            if not self.has_session(req):
                return Response.json_({"error": "session expired"}, 401)
            return self.api(req)
        if not self.has_session(req):
            target = p + ("?" + "&".join(f"{k}={v}" for k, v in req.query.items()) if req.query else "")
            return Response.redirect("/login?next=" + urlquote(target, safe=""))
        if p == "/quote":
            schema = json.dumps({"steps": STEPS}).replace("</", "<\\/")
            body = (
                "<div class=wrap><nav class=steps id=steps aria-label='Quote steps'></nav>"
                "<main id=app aria-live=polite><p>Loading…</p></main></div><dialog id=estdlg></dialog>"
                f"<script type='application/json' id=schema>{schema}</script><script>{APP_JS}</script>"
            )
            return Response.html(page("Homeowners quote – Corvane AgentLink", body, self.creds["username"]))
        if p.startswith("/quote/result/"):
            return self.result(p.rsplit("/", 1)[-1])
        return Response.html(page("Not found", "<main><h1>Not found</h1></main>"), 404)

    def login(self, req: Request) -> Response:
        nxt = req.query.get("next") or req.form.get("next") or "/quote"
        if not nxt.startswith("/"):
            nxt = "/quote"
        msg = ""
        if req.method == "POST":
            if self.check_login(req.form.get("username", ""), req.form.get("password", "")):
                sid = self.new_session()
                return Response.redirect(nxt).set_cookie(self.cookie, sid)
            msg = "<p class=err role=alert>That agency code and password do not match.</p>"
        elif req.query.get("expired"):
            msg = "<div class=banner role=status>Your session expired. Sign in again to continue.</div>"
        body = (
            "<div class=wrap><main style='max-width:420px;margin:0 auto'><h1>Agent sign-in</h1>"
            f"{msg}<form method=post action='/login'><input type=hidden name=next value='{esc(nxt)}'>"
            "<div class=fld><label for=u>Agency code</label><input type=text id=u name=username autocomplete=username></div>"
            "<div class=fld><label for=pw>Password</label><input type=password id=pw name=password "
            "style='width:100%;padding:7px 9px'></div><button type=submit>Sign in</button></form></main></div>"
        )
        return Response.html(page("Sign in – Corvane AgentLink", body))

    def api(self, req: Request) -> Response:
        p = req.path
        with self.lock:
            if p == "/api/quote" and req.method == "GET":
                return Response.json_(self._state())
            if p.startswith("/api/quote/step/") and req.method == "PUT":
                n = parse_int(p.rsplit("/", 1)[-1]) or 0
                if n not in STEP_FIELDS:
                    return Response.json_({"error": "bad step"}, 400)
                if n == 4 and self.fire_once("save_step4"):
                    self.drop_sessions()
                    self._revert_after_expiry()
                    return Response.json_({"error": "session expired"}, 401)
                vals = (req.json or {}).get("values", {})
                clean, errors = self._validate(n, vals)
                if errors:
                    return Response.json_({"errors": errors})
                self.draft.update(clean)
                if n not in self.saved:
                    self.saved.append(n)
                if n == 1 and not self.records_applied:
                    self._apply_public_record()
                return Response.json_({"ok": True, "state": self._state(), "next_step": n + 1})
            if p == "/api/lenders":
                q = req.query.get("q", "").strip().casefold()
                matches = [c for c in CV_LENDERS if q and q in c.split(" ISAOA")[0].casefold()]
                return Response.json_({"matches": matches})
            if p == "/api/estimate":
                sq = parse_int(self.draft.get("square_feet"))
                cons = self.draft.get("construction") or ""
                if 2 not in self.saved or not sq or not cons:
                    return Response.json_({"error": "Save the Dwelling step first."})
                est = replacement_cost(sq, cons)
                apply_v = est["base"] if self.config.get("estimator_applies") == "base" else est["total"]
                return Response.json_({
                    "square_feet": f"{sq:,}", "construction": cons,
                    "base_display": money(est["base"]), "allowance_display": money(est["allowance"]),
                    "total_display": money(est["total"]), "total": est["total"],
                    "apply_display": f"{apply_v:,}",
                })
            if p == "/api/quote/submit" and req.method == "POST":
                missing = [n for n in (1, 2, 3, 4) if n not in self.saved]
                if missing:
                    return Response.json_({"error": f"Complete step {missing[0]} first."})
                rec = self._record()
                self.quote_seq += 1
                rec["quote_ref"] = f"CVH-Q{27000 + self.quote_seq:06d}"
                rec["annual_premium"] = self._premium(rec)
                stored = self.store_submission(rec)
                self.quotes[rec["quote_ref"]] = stored
                return Response.json_({"ok": True, "quote_ref": rec["quote_ref"]})
        return Response.json_({"error": "not found"}, 404)

    def result(self, ref: str) -> Response:
        rec = self.quotes.get(ref)
        if not rec:
            return Response.html(page("Not found", "<main><h1>Quote not found</h1></main>"), 404)
        rows = "".join(
            f"<tr data-name='{k}'><th>{esc(FIELD_DEF[k]['label'])}</th>"
            f"<td>{esc(_display(k, 'Yes' if rec[k] is True else 'No' if rec[k] is False else rec[k]))}</td></tr>"
            for k in RECORD_FIELDS
        )
        body = (
            "<div class=wrap><main><h1>Quote ready</h1>"
            f"<p>Quote reference <strong id=quote-ref>{esc(ref)}</strong> · "
            f"Estimated annual premium <strong id=premium>{esc(money(rec['annual_premium']))}</strong></p>"
            f"<table class=sum id=stored-quote>{rows}</table></main></div>"
        )
        return Response.html(page(f"Quote {ref} – Corvane AgentLink", body, self.creds["username"]))
