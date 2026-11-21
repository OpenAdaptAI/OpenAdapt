"""Pinecrest Assurance: agent workspace with the quote app in an iframe.

The outer page (``/portal``) is a shell; the homeowners app lives in a
same-origin ``<iframe>``. The app re-renders the whole step from state on
every ``change`` event (element ids are regenerated each render), uses a
type-ahead city search whose listbox is appended to the frame's body, and a
rebuild-cost calculator in a modal ``<dialog>``.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from .common import BasePortal, Request, Response, esc, parse_date, parse_int, slow
from .estimators import pinecrest_rebuild

CITIES = [
    ("Bayard Crossing", "NC"), ("Fairview", "NC"), ("Fairview", "OR"), ("Fairview", "TN"),
    ("Hazel Bluff", "OR"), ("Quarry Point", "WA"), ("Saltmarsh", "MA"), ("Springfield", "IL"),
    ("Springfield", "MA"), ("Springfield", "MO"), ("Springfield", "OR"), ("Tamarack Ridge", "ID"),
]
CONSTRUCTION = ["Wood frame", "Brick / masonry", "Steel / concrete", "Log"]
STORIES = ["1", "2", "3+"]
ROOF = ["Asphalt shingle", "Metal", "Tile", "Slate", "Wood shake"]
HEATING = ["Gas furnace", "Electric baseboard", "Heat pump", "Oil boiler", "Wood stove"]
FINISH = ["Builder grade", "Semi-custom", "Custom", "Luxury"]
DEDUCTIBLES = ["1000", "2500", "5000"]
SQM_TO_SQFT = 10.7639

STEP_FIELDS = {
    1: ["street", "city", "state", "zip", "year_built", "area", "area_unit", "construction", "stories"],
    2: ["roof_year", "roof_material", "heating", "stove_installed", "prior_claims", "burglar_alarm"],
    3: ["dwelling_limit", "deductible", "effective_date"],
}
RECORD_FIELDS = [
    "street", "city", "state", "zip", "year_built", "living_area_sqft", "construction", "stories",
    "roof_year", "roof_material", "heating", "stove_installed", "prior_claims", "burglar_alarm",
    "dwelling_limit", "deductible", "effective_date",
]
REVIEW = [
    ("Property", 1, [("street", "Street address"), ("city", "City"), ("zip", "ZIP code"), ("year_built", "Year built"),
                     ("living_area_sqft", "Living area"), ("construction", "Construction type"), ("stories", "Stories")]),
    ("Roof and systems", 2, [("roof_year", "Year roof was installed"), ("roof_material", "Roof material"),
                             ("heating", "Primary heating"),
                             ("stove_installed", "Was the wood stove installed by a licensed professional?"),
                             ("prior_claims", "Claims in the last 5 years"),
                             ("burglar_alarm", "Monitored burglar alarm")]),
    ("Coverage", 3, [("dwelling_limit", "Dwelling limit"), ("deductible", "Deductible"),
                     ("effective_date", "Policy start date")]),
]


def parse_limit(text: Any) -> int | None:
    s = str(text or "").strip().replace("$", "").replace(",", "").replace(" ", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KkMm]?)", s)
    if not m:
        return None
    mult = {"": 1, "k": 1000, "m": 1000000}[m.group(2).lower()]
    return int(round(float(m.group(1)) * mult))


def sqft_of(area: Any, unit: str) -> int | None:
    a = parse_int(area)
    if a is None:
        return None
    return int(round(a * SQM_TO_SQFT)) if unit == "m2" else a


class Pinecrest(BasePortal):
    portal_id = "pinecrest"
    cookie = "pcauth"

    def fresh_draft(self) -> dict[str, Any]:
        d: dict[str, Any] = {f: "" for n in STEP_FIELDS for f in STEP_FIELDS[n]}
        d.update(area_unit="sqft", burglar_alarm=False, deductible="1000", _step=1, _notice="")
        prior = getattr(self, "config", {}).get("prior_policy")
        if prior:
            d.update(json.loads(json.dumps(prior["fields"])))
            d["_notice"] = (
                f"Details were imported from the client's current policy {prior['number']}. "
                "Confirm each one with the client."
            )
        return d

    def route(self, req: Request) -> Response:
        p = req.path
        if p in ("/", "/login"):
            return self.login(req)
        if p == "/logout":
            return Response.redirect("/login").clear_cookie(self.cookie)
        if not self.has_session(req):
            if p.startswith("/app/api/"):
                return Response.json_({"error": "session_expired"}, 401)
            return Response.redirect("/login?next=" + quote(p))
        if p == "/portal":
            return Response.html(SHELL)
        if p in ("/app", "/app/"):
            return Response.html(APP)
        if p == "/app/api/draft":
            with self.lock:
                return Response.json_({"step": self.draft["_step"], "notice": self.draft["_notice"],
                                       "data": {k: self.draft[k] for n in STEP_FIELDS for k in STEP_FIELDS[n]}})
        if p == "/app/api/suggest":
            slow(0.25)
            q = req.query.get("q", "").split(",")[0].strip().casefold()
            out = [{"label": f"{c}, {s}", "city": c, "state": s} for c, s in CITIES if q and c.casefold().startswith(q)]
            return Response.json_(out)
        if p == "/app/api/estimate" and req.method == "POST":
            b = req.json or {}
            sq, yb = parse_int(b.get("sqft")), parse_int(b.get("year_built"))
            if not sq or not yb or b.get("construction") not in CONSTRUCTION or b.get("finish") not in FINISH:
                return Response.json_({"error": "Complete every input."}, 422)
            return Response.json_({"value": pinecrest_rebuild(sq, yb, b["construction"], b["finish"])})
        if p == "/app/api/save" and req.method == "POST":
            return self.save(req)
        if p == "/app/api/review":
            with self.lock:
                return Response.json_({"sections": self.sections(self.record_of(self.draft))})
        if p == "/app/api/submit" and req.method == "POST":
            return self.submit()
        m = re.fullmatch(r"/app/api/quote/(PQ-\d+)", p)
        if m:
            rec = next((r for r in self.submissions if r.get("quote_ref") == m.group(1)), None)
            if rec is None:
                return Response.json_({"error": "not found"}, 404)
            return Response.json_({"quote_ref": rec["quote_ref"], "sections": self.sections(rec)})
        return Response.html("<h1>Not found</h1>", 404)

    def login(self, req: Request) -> Response:
        nxt = req.query.get("next") or req.form.get("next") or "/portal"
        if not nxt.startswith("/") or nxt.startswith("/app"):
            nxt = "/portal"
        msg = ""
        if req.method == "POST":
            if self.check_login(req.form.get("username", ""), req.form.get("password", "")):
                return Response.redirect(nxt).set_cookie(self.cookie, self.new_session())
            msg = "<p class='bad' role='alert'>Incorrect username or password.</p>"
        elif req.query.get("expired"):
            msg = "<p class='bad' role='alert'>Your session ended. Sign in again to continue.</p>"
        return Response.html(LOGIN.replace("__MSG__", msg).replace("__NEXT__", esc(nxt)))

    # ------------------------------------------------------------------
    def _validate(self, n: int, d: dict[str, Any]) -> dict[str, str]:
        e: dict[str, str] = {}
        req = "Required"
        if n == 1:
            if not str(d.get("street", "")).strip():
                e["street"] = req
            if (d.get("city"), d.get("state")) not in CITIES:
                e["city"] = "Pick a city from the list"
            if not re.fullmatch(r"\d{5}", str(d.get("zip", ""))):
                e["zip"] = "5-digit ZIP"
            yb = parse_int(d.get("year_built"))
            if yb is None or not 1800 <= yb <= 2026:
                e["year_built"] = "Enter a year"
            if parse_int(d.get("area")) is None or d.get("area_unit") not in ("sqft", "m2"):
                e["area"] = req
            if d.get("construction") not in CONSTRUCTION:
                e["construction"] = req
            if d.get("stories") not in STORIES:
                e["stories"] = req
        elif n == 2:
            ry = parse_int(d.get("roof_year"))
            if ry is None or not 1900 <= ry <= 2026:
                e["roof_year"] = "Enter a year"
            if d.get("roof_material") not in ROOF:
                e["roof_material"] = req
            if d.get("heating") not in HEATING:
                e["heating"] = req
            if d.get("heating") == "Wood stove" and d.get("stove_installed") not in ("Yes", "No"):
                e["stove_installed"] = req
            if parse_int(d.get("prior_claims")) is None:
                e["prior_claims"] = req
        elif n == 3:
            if parse_limit(d.get("dwelling_limit")) is None:
                e["dwelling_limit"] = "Enter an amount"
            if d.get("deductible") not in DEDUCTIBLES:
                e["deductible"] = req
            if not parse_date(str(d.get("effective_date", ""))):
                e["effective_date"] = "Enter a date"
        return e

    def save(self, req: Request) -> Response:
        b = req.json or {}
        n = int(b.get("step", 0))
        data = b.get("data") or {}
        if n not in STEP_FIELDS:
            return Response.json_({"error": "bad step"}, 400)
        with self.lock:
            if not b.get("partial") and self.fire_once(f"save_step_{n}"):
                for k, v in self.config.get("reset_on_resume", {}).items():
                    self.draft[k] = v
                self.draft["_step"] = n
                self.drop_sessions()
                return Response.json_({"error": "session_expired"}, 401)
            for f in STEP_FIELDS[n]:
                if f in data:
                    v = data[f]
                    self.draft[f] = bool(v) if f == "burglar_alarm" else " ".join(str(v).split())
            if self.draft.get("heating") != "Wood stove":
                self.draft["stove_installed"] = ""
            if b.get("partial"):
                self.draft["_step"] = max(1, n - 1)
                return Response.json_({"ok": True})
            errors = self._validate(n, self.draft)
            if errors:
                return Response.json_({"ok": False, "errors": errors}, 422)
            self.draft["_step"] = n + 1
            return Response.json_({"ok": True})

    def record_of(self, d: dict[str, Any]) -> dict[str, Any]:
        rec: dict[str, Any] = {}
        for f in RECORD_FIELDS:
            if f == "living_area_sqft":
                v = sqft_of(d.get("area"), d.get("area_unit", "sqft"))
                rec[f] = "" if v is None else v
            elif f in ("year_built", "roof_year", "prior_claims"):
                v = parse_int(d.get(f))
                rec[f] = "" if v is None else v
            elif f == "dwelling_limit":
                v = parse_limit(d.get(f))
                rec[f] = "" if v is None else v
            elif f == "effective_date":
                rec[f] = parse_date(str(d.get(f, "")))
            else:
                rec[f] = d.get(f, "")
        return rec

    def _display(self, f: str, v: Any, rec: dict[str, Any]) -> str:
        if f == "burglar_alarm":
            return "Yes" if v else "No"
        if v in ("", None):
            return ""
        if f == "city":
            return f"{v}, {rec.get('state', '')}"
        if f == "living_area_sqft":
            return f"{int(v):,} sq ft"
        if f in ("dwelling_limit", "deductible"):
            return f"${int(v):,}"
        if f == "effective_date":
            y, m, dd = str(v).split("-")
            return f"{m}/{dd}/{y}"
        return str(v)

    def sections(self, rec: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for title, step, fields in REVIEW:
            rows = []
            for f, label in fields:
                if f == "stove_installed" and rec.get("heating") != "Wood stove":
                    continue
                val = self._display(f, rec.get(f), rec)
                if f == "living_area_sqft" and "quote_ref" not in rec and self.draft.get("area_unit") == "m2":
                    val = f"{self.draft.get('area')} m² ({val})"
                rows.append({"label": label, "value": val})
            out.append({"title": title, "step": step, "rows": rows})
        return out

    def submit(self) -> Response:
        with self.lock:
            for n in STEP_FIELDS:
                if self._validate(n, self.draft):
                    return Response.json_({"ok": False, "error": f"Section {n} is incomplete.", "step": n}, 422)
            self.quote_seq += 1
            rec = self.record_of(self.draft)
            rec["quote_ref"] = f"PQ-{88100 + self.quote_seq}"
            self.store_submission(rec)
            return Response.json_({"ok": True, "quote_ref": rec["quote_ref"]})


_META = json.dumps({"construction": CONSTRUCTION, "stories": STORIES, "roof": ROOF, "heating": HEATING,
                    "finish": FINISH, "deductibles": DEDUCTIBLES})

LOGIN = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Sign in | Pinecrest Assurance</title>
<style>body{font-family:Georgia,serif;background:#eef2ea;color:#1d2b1f;margin:0}
.box{max-width:360px;margin:80px auto;background:#fff;border:1px solid #c9d3c4;padding:28px;border-radius:6px}
h1{font-size:22px;margin:0 0 16px;color:#24502c}label{display:block;margin:12px 0 4px;font-family:sans-serif;font-size:14px}
input{width:100%;padding:8px;box-sizing:border-box}button{margin-top:16px;padding:9px 18px;background:#24502c;color:#fff;border:0;border-radius:4px}
.bad{color:#a12a1a;font-family:sans-serif}</style></head><body><div class="box">
<h1>Pinecrest Assurance</h1><p style="font-family:sans-serif;font-size:13px">Agent workspace sign-in</p>__MSG__
<form method="post" action="/login"><input type="hidden" name="next" value="__NEXT__">
<label for="u">Username</label><input id="u" name="username" autocomplete="username">
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password">
<button type="submit">Sign in</button></form></div></body></html>"""

SHELL = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Workspace | Pinecrest Assurance</title>
<style>body{margin:0;font-family:sans-serif;background:#eef2ea}header{background:#24502c;color:#fff;padding:10px 18px;display:flex;justify-content:space-between}
nav a{color:#d6e6d0;margin-left:14px;font-size:14px}iframe{border:0;width:100%;height:calc(100vh - 44px);display:block}</style></head>
<body><header><strong>Pinecrest Assurance &middot; Agent Workspace</strong><nav><a href="#">Book of business</a><a href="#">Service</a><a href="/logout">Sign out</a></nav></header>
<iframe id="quote-frame" title="Homeowners quote" src="/app/"></iframe></body></html>"""

APP = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Homeowners quote</title>
<style>
body{margin:0;font-family:Georgia,serif;color:#1d2b1f;background:#fbfcf9}
#root{max-width:760px;margin:20px auto;padding:0 18px}
.tabs{display:flex;gap:0;margin-bottom:14px;font-family:sans-serif;font-size:13px}
.tabs span{padding:6px 12px;border-bottom:3px solid #d9e1d4;color:#6b7a6c}.tabs span.on{border-color:#24502c;color:#24502c;font-weight:700}
h2{font-size:20px;color:#24502c;margin:6px 0 14px}
.pc-row{display:grid;grid-template-columns:240px 1fr;gap:10px;align-items:start;padding:8px 0;border-bottom:1px dashed #dde4d8}
.pc-row label,.pc-row .pc-q{font-family:sans-serif;font-size:14px;padding-top:6px}
.pc-row input[type=text],.pc-row input[type=date],.pc-row select{padding:7px;border:1px solid #b9c5b3;border-radius:4px;font:inherit;font-family:sans-serif;min-width:220px}
.pc-err{color:#a12a1a;font-family:sans-serif;font-size:12px;grid-column:2}
.pc-banner{background:#fff6d8;border:1px solid #e2cf86;padding:8px 12px;font-family:sans-serif;font-size:13px;margin-bottom:12px}
.pc-actions{margin-top:16px;display:flex;gap:10px}
button{font-family:sans-serif;padding:8px 16px;border-radius:4px;border:1px solid #24502c;background:#fff;color:#24502c;cursor:pointer}
button.go{background:#24502c;color:#fff}
.pc-list{position:absolute;list-style:none;margin:0;padding:0;background:#fff;border:1px solid #9fb19a;font-family:sans-serif;font-size:14px;z-index:20}
.pc-list li{padding:6px 10px;cursor:pointer}.pc-list li:hover{background:#e6efe2}
.pc-cards{display:flex;gap:8px}.pc-cards label{border:1px solid #b9c5b3;padding:6px 12px;border-radius:4px;padding-top:6px}
dialog{border:1px solid #9fb19a;border-radius:6px;padding:20px;width:420px}dialog::backdrop{background:rgba(20,40,20,.4)}
dialog .pc-row{grid-template-columns:170px 1fr}
table.rv{border-collapse:collapse;width:100%;font-family:sans-serif;font-size:14px}table.rv th{text-align:left;width:46%;color:#55635a;font-weight:normal}
table.rv td,table.rv th{padding:5px 4px;border-bottom:1px solid #e4e9e0}
</style></head><body><div id="root">Loading...</div>
<dialog id="pc-est" aria-labelledby="pc-est-title"><h2 id="pc-est-title">Rebuild cost calculator</h2><div id="pc-est-body"></div></dialog>
<script>
const META = __META__;
let S = {step: 1, data: {}, errors: {}, notice: ''};
const rid = () => Math.random().toString(36).slice(2, 8);
const h = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url, body) {
  const r = await fetch(url, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  if (r.status === 401) { expired(); throw new Error('auth'); }
  return {status: r.status, body: await r.json()};
}
function expired() {
  document.getElementById('root').innerHTML = '<h2>Session ended</h2><p>Your workspace session ended. <a id="pc-relogin" target="_top" href="/login?expired=1&next=/portal">Sign in again</a> to continue this quote.</p>';
}
async function load() { const r = await api('/app/api/draft'); S.step = r.body.step; S.data = r.body.data; S.notice = r.body.notice; S.errors = {}; render(); }
function row(key, label, ctl) {
  const e = S.errors[key] ? '<div class="pc-err" role="alert">' + h(S.errors[key]) + '</div>' : '';
  return '<div class="pc-row" data-key="' + key + '">' + label + ctl + e + '</div>';
}
function text(key, label, type) { const id = 'f' + rid(); return row(key, '<label for="' + id + '">' + h(label) + '</label>', '<div><input type="' + (type || 'text') + '" id="' + id + '" name="' + key + '-' + rid() + '" data-f="' + key + '" value="' + h(S.data[key]) + '"></div>'); }
function sel(key, label, opts, extra) { const id = 'f' + rid(); return row(key, '<label for="' + id + '">' + h(label) + '</label>', '<div><select id="' + id + '" data-f="' + key + '"><option value="">Choose...</option>' + opts.map(o => { const v = Array.isArray(o) ? o[0] : o, t = Array.isArray(o) ? o[1] : o; return '<option value="' + h(v) + '"' + (String(S.data[key]) === v ? ' selected' : '') + '>' + h(t) + '</option>'; }).join('') + '</select>' + (extra || '') + '</div>'); }
function radios(key, label, opts, fmt) { const gid = 'g' + rid(); return row(key, '<div class="pc-q" id="' + gid + '">' + h(label) + '</div>', '<div class="pc-cards" role="radiogroup" aria-labelledby="' + gid + '">' + opts.map(o => { const id = 'r' + rid(); return '<label for="' + id + '"><input type="radio" id="' + id + '" name="' + key + gid + '" data-f="' + key + '" value="' + h(o) + '"' + (S.data[key] === o ? ' checked' : '') + '> ' + h(fmt ? fmt(o) : o) + '</label>'; }).join('') + '</div>'); }
function tabs() { return '<div class="tabs">' + ['Property', 'Roof and systems', 'Coverage', 'Review'].map((t, i) => '<span class="' + (i + 1 === S.step ? 'on' : '') + '">' + (i + 1) + '. ' + t + '</span>').join('') + '</div>'; }
function render() {
  document.querySelectorAll('.pc-list').forEach(x => x.remove());
  const d = S.data; let html = tabs();
  if (S.notice && S.step <= 2) html += '<div class="pc-banner" role="status">' + h(S.notice) + '</div>';
  if (S.step === 1) {
    html += '<h2>Property</h2>' + text('street', 'Street address');
    const cid = 'f' + rid(), lb = 'lb' + rid();
    html += row('city', '<label for="' + cid + '">City</label>', '<div><input type="text" id="' + cid + '" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="' + lb + '" autocomplete="off" data-combo="city" value="' + h(d.city ? d.city + ', ' + d.state : '') + '"></div>');
    html += text('zip', 'ZIP code') + text('year_built', 'Year built');
    const aid = 'f' + rid(), uid = 'u' + rid();
    html += row('area', '<label for="' + aid + '">Living area</label>', '<div><input type="text" id="' + aid + '" data-f="area" value="' + h(d.area) + '" style="min-width:120px"> <label for="' + uid + '" style="font-size:12px">Area unit</label> <select id="' + uid + '" data-f="area_unit"><option value="sqft"' + (d.area_unit === 'sqft' ? ' selected' : '') + '>sq ft</option><option value="m2"' + (d.area_unit === 'm2' ? ' selected' : '') + '>m\u00b2</option></select></div>');
    html += sel('construction', 'Construction type', META.construction) + radios('stories', 'Stories', META.stories);
  } else if (S.step === 2) {
    html += '<h2>Roof and systems</h2>' + text('roof_year', 'Year roof was installed') + sel('roof_material', 'Roof material', META.roof) + sel('heating', 'Primary heating', META.heating);
    if (d.heating === 'Wood stove') html += radios('stove_installed', 'Was the wood stove installed by a licensed professional?', ['Yes', 'No']);
    html += text('prior_claims', 'Claims in the last 5 years');
    const aid = 'f' + rid();
    html += row('burglar_alarm', '<label for="' + aid + '">Monitored burglar alarm</label>', '<div><input type="checkbox" id="' + aid + '" data-f="burglar_alarm"' + (d.burglar_alarm ? ' checked' : '') + '></div>');
  } else if (S.step === 3) {
    html += '<h2>Coverage</h2>';
    const did = 'f' + rid();
    html += row('dwelling_limit', '<label for="' + did + '">Dwelling limit</label>', '<div><input type="text" id="' + did + '" data-f="dwelling_limit" value="' + h(d.dwelling_limit) + '"> <button type="button" id="pc-calc">Calculate rebuild cost</button></div>');
    html += radios('deductible', 'Deductible', META.deductibles, v => '$' + Number(v).toLocaleString('en-US'));
    html += text('effective_date', 'Policy start date', 'date');
  } else if (S.step === 4) { review(); return; }
  html += '<div class="pc-actions">' + (S.step > 1 ? '<button type="button" id="pc-back">Back</button>' : '') + '<button type="button" class="go" id="pc-next">Save and continue</button></div>';
  document.getElementById('root').innerHTML = html;
  wire();
}
function wire() {
  const root = document.getElementById('root');
  root.querySelectorAll('[data-f]').forEach(c => {
    const k = c.dataset.f;
    const read = () => c.type === 'checkbox' ? c.checked : c.value;
    if (c.type !== 'radio') c.addEventListener('input', () => { S.data[k] = read(); });
    c.addEventListener('change', () => { S.data[k] = read(); render(); });
  });
  const combo = root.querySelector('[data-combo]');
  if (combo) wireCombo(combo);
  const calc = document.getElementById('pc-calc'); if (calc) calc.addEventListener('click', openCalc);
  const back = document.getElementById('pc-back'); if (back) back.addEventListener('click', async () => { try { await api('/app/api/save', {step: S.step, data: S.data, partial: true}); } catch (e) { return; } await load(); });
  document.getElementById('pc-next').addEventListener('click', async () => {
    let r; try { r = await api('/app/api/save', {step: S.step, data: S.data}); } catch (e) { return; }
    if (r.status === 422) { S.errors = r.body.errors; render(); return; }
    await load();
  });
}
function wireCombo(inp) {
  let t = null, seq = 0; const lb = inp.getAttribute('aria-controls');
  const close = () => { const o = document.getElementById(lb); if (o) o.remove(); inp.setAttribute('aria-expanded', 'false'); };
  inp.addEventListener('input', () => {
    S.data.city = ''; S.data.state = ''; clearTimeout(t);
    t = setTimeout(async () => {
      const mine = ++seq; let r; try { r = await api('/app/api/suggest?q=' + encodeURIComponent(inp.value)); } catch (e) { return; }
      if (mine !== seq) return; close();
      const ul = document.createElement('ul'); ul.id = lb; ul.className = 'pc-list'; ul.setAttribute('role', 'listbox');
      const b = inp.getBoundingClientRect(); ul.style.left = (b.left + scrollX) + 'px'; ul.style.top = (b.bottom + scrollY) + 'px'; ul.style.minWidth = b.width + 'px';
      r.body.forEach((it, i) => { const li = document.createElement('li'); li.setAttribute('role', 'option'); li.id = lb + '-' + i; li.textContent = it.label;
        li.addEventListener('mousedown', ev => { ev.preventDefault(); S.data.city = it.city; S.data.state = it.state; close(); render(); }); ul.append(li); });
      document.body.append(ul); inp.setAttribute('aria-expanded', 'true');
    }, 120);
  });
  inp.addEventListener('blur', () => setTimeout(close, 250));
}
function openCalc() {
  const d = S.data; const dlg = document.getElementById('pc-est');
  const area = Number(String(d.area || '').replace(/,/g, ''));
  const sq = d.area_unit === 'm2' ? Math.round(area * 10.7639) : area;
  const ids = {sq: 'e' + rid(), yb: 'e' + rid(), c: 'e' + rid(), f: 'e' + rid()};
  const opt = (list, cur) => '<option value="">Choose...</option>' + list.map(o => '<option' + (o === cur ? ' selected' : '') + '>' + h(o) + '</option>').join('');
  document.getElementById('pc-est-body').innerHTML =
    '<div class="pc-row"><label for="' + ids.sq + '">Living area (sq ft)</label><input type="text" id="' + ids.sq + '" value="' + h(sq || '') + '"></div>' +
    '<div class="pc-row"><label for="' + ids.yb + '">Year built</label><input type="text" id="' + ids.yb + '" value="' + h(d.year_built) + '"></div>' +
    '<div class="pc-row"><label for="' + ids.c + '">Construction type</label><select id="' + ids.c + '">' + opt(META.construction, d.construction) + '</select></div>' +
    '<div class="pc-row"><label for="' + ids.f + '">Finish level</label><select id="' + ids.f + '">' + opt(META.finish, '') + '</select></div>' +
    '<p id="pc-est-out" aria-live="polite"></p><div class="pc-actions"><button type="button" id="pc-est-go" class="go">Calculate</button><button type="button" id="pc-est-apply" disabled>Apply to dwelling limit</button><button type="button" id="pc-est-close">Close</button></div>';
  let value = null;
  document.getElementById('pc-est-go').onclick = async () => {
    const g = id => document.getElementById(id).value;
    let r; try { r = await api('/app/api/estimate', {sqft: g(ids.sq), year_built: g(ids.yb), construction: g(ids.c), finish: g(ids.f)}); } catch (e) { return; }
    const out = document.getElementById('pc-est-out');
    if (r.status !== 200) { out.textContent = r.body.error; return; }
    value = r.body.value;
    out.innerHTML = 'Estimated rebuild cost: <strong id="pc-estimate">$' + value.toLocaleString('en-US') + '</strong>';
    document.getElementById('pc-est-apply').disabled = false;
  };
  document.getElementById('pc-est-apply').onclick = () => {
    S.data.dwelling_limit = value >= 1000000 ? (value / 1000000).toFixed(2) + 'M' : value.toLocaleString('en-US');
    dlg.close(); render();
  };
  document.getElementById('pc-est-close').onclick = () => dlg.close();
  dlg.showModal();
}
async function review() {
  let r; try { r = await api('/app/api/review'); } catch (e) { return; }
  let html = tabs() + '<h2>Review</h2>';
  r.body.sections.forEach(s => { html += '<h3 style="font-family:sans-serif;font-size:15px">' + h(s.title) + ' <button type="button" data-edit="' + s.step + '">Edit</button></h3><table class="rv">' + s.rows.map(x => '<tr><th>' + h(x.label) + '</th><td>' + h(x.value) + '</td></tr>').join('') + '</table>'; });
  html += '<p id="pc-submit-err" class="pc-err"></p><div class="pc-actions"><button type="button" id="pc-back">Back</button><button type="button" class="go" id="pc-submit">Submit for quote</button></div>';
  document.getElementById('root').innerHTML = html;
  document.querySelectorAll('[data-edit]').forEach(b => b.addEventListener('click', () => { S.step = Number(b.dataset.edit); render(); }));
  document.getElementById('pc-back').addEventListener('click', () => { S.step = 3; render(); });
  document.getElementById('pc-submit').addEventListener('click', async () => {
    let r; try { r = await api('/app/api/submit', {}); } catch (e) { return; }
    if (r.status !== 200) { document.getElementById('pc-submit-err').textContent = r.body.error; return; }
    const q = await api('/app/api/quote/' + r.body.quote_ref);
    let out = '<h2>Quote request received</h2><p style="font-family:sans-serif">Reference <strong id="pc-ref">' + h(r.body.quote_ref) + '</strong>. Values sent to rating:</p>';
    q.body.sections.forEach(s => { out += '<table class="rv">' + s.rows.map(x => '<tr><th>' + h(x.label) + '</th><td>' + h(x.value) + '</td></tr>').join('') + '</table>'; });
    document.getElementById('root').innerHTML = out;
  });
}
load().catch(() => {});
</script></body></html>""".replace("__META__", _META)
