"""Cobalt Ridge Insurance: consumer auto quote, single-page app.

Div/ARIA markup rendered by vanilla JS from a JSON API: no ``<label for>``,
custom radio groups (``role=radiogroup``), and type-ahead comboboxes whose
suggestions arrive asynchronously and render in a listbox appended to
``document.body``. Each step is saved server-side with ``PUT /api/step/<n>``;
the review page is built from the server's saved draft.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .common import BasePortal, Request, Response, parse_date, parse_int, slow

STATES = [
    ("AZ", "Arizona"), ("CA", "California"), ("CO", "Colorado"), ("ID", "Idaho"), ("IL", "Illinois"),
    ("MA", "Massachusetts"), ("MO", "Missouri"), ("NC", "North Carolina"), ("NV", "Nevada"),
    ("NH", "New Hampshire"), ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
    ("OR", "Oregon"), ("PA", "Pennsylvania"), ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"),
    ("WA", "Washington"),
]
STATE_NAME = dict(STATES)
MODELS = {
    "Halden": ["Fjord", "Fjord Touring", "Skerry"],
    "Kestrel": ["Corsa Sport", "Corsa", "Corsa Hybrid", "Ridgeback", "Ridgeback XL"],
    "Marlowe": ["Avenue", "Avenue LX", "Brook"],
    "Norvik": ["Polar", "Polar Trail", "Aster"],
    "Solenne": ["Vida Cross", "Vida", "Lune"],
}
CITIES = [
    ("Brindle Falls", "ID"), ("Cedar Hollow", "OR"), ("Fairview", "NC"), ("Fairview", "OR"),
    ("Fairview", "PA"), ("Fairview", "TN"), ("Lakemont", "WA"), ("Port Alder", "WA"),
    ("Springfield", "IL"), ("Springfield", "MA"), ("Springfield", "MO"), ("Springfield", "OR"),
    ("Juniper Flats", "NV"), ("Copper Mesa", "AZ"),
]
MARITAL = ["Single", "Married", "Divorced", "Widowed"]
RELATIONSHIP = ["Spouse", "Domestic partner", "Child", "Parent", "Other"]
USES = ["Commute", "Pleasure", "Business"]
OWNERSHIP = ["Own", "Finance", "Lease"]
BI = ["25/50", "50/100", "100/300", "250/500"]
DEDS = ["No coverage", "250", "500", "1000"]
YEARS = [str(y) for y in range(2026, 2009, -1)]

STEP_FIELDS = {
    1: ["first_name", "last_name", "dob", "email", "license_state", "marital_status"],
    2: ["additional_drivers"],
    3: ["vehicle_year", "make", "model", "use", "commute_miles", "rideshare", "annual_miles",
        "garaging_zip", "garaging_city", "garaging_state", "ownership", "lienholder"],
    4: ["bi_limits", "collision_ded", "comp_ded"],
}
RECORD_FIELDS = [f for n in sorted(STEP_FIELDS) for f in STEP_FIELDS[n]]
DRIVER_FIELDS = ["first_name", "last_name", "dob", "relationship"]

REVIEW_LABELS = [
    ("About you", [("first_name", "First name"), ("last_name", "Last name"), ("dob", "Date of birth"),
                   ("email", "Email address"), ("license_state", "State where licensed"),
                   ("marital_status", "Marital status")]),
    ("Vehicle", [("vehicle_year", "Model year"), ("make", "Make"), ("model", "Model"), ("use", "Primary use"),
                 ("commute_miles", "One-way commute (miles)"), ("rideshare", "Used for rideshare or delivery?"),
                 ("annual_miles", "Estimated annual mileage"), ("garaging_zip", "Garaging ZIP code"),
                 ("garaging_city", "Garaging city"), ("ownership", "Ownership"),
                 ("lienholder", "Lienholder or lessor name")]),
    ("Coverage", [("bi_limits", "Bodily injury liability"), ("collision_ded", "Collision deductible"),
                  ("comp_ded", "Comprehensive deductible")]),
]


def _ded_label(v: str) -> str:
    return v if v in ("", "No coverage") else f"${int(v):,}"


class Cobalt(BasePortal):
    portal_id = "cobalt"
    cookie = "crsid"

    def fresh_draft(self) -> dict[str, Any]:
        d: dict[str, Any] = {f: "" for f in RECORD_FIELDS}
        d["additional_drivers"] = []
        d["collision_ded"] = "500"
        d["comp_ded"] = "500"
        d["_step"] = 1
        d["_started"] = False
        d["_notice"] = ""
        d["_prefilled"] = False
        return d

    # ------------------------------------------------------------------
    def route(self, req: Request) -> Response:
        p = req.path
        if p in ("/", "/quote", "/quote/"):
            resp = Response.html(SHELL)
            if not self.has_session(req) and not self.draft["_started"]:
                resp.set_cookie(self.cookie, self.new_session())
            return resp
        if p == "/api/login" and req.method == "POST":
            body = req.json or {}
            if self.check_login(body.get("username", ""), body.get("password", "")):
                return Response.json_({"ok": True}).set_cookie(self.cookie, self.new_session())
            return Response.json_({"ok": False, "error": "That email and password do not match our records."}, 401)
        if not p.startswith("/api/"):
            return Response.html("<h1>Not found</h1>", 404)
        if not self.has_session(req):
            return Response.json_({"error": "session_expired"}, 401)
        if p == "/api/draft":
            with self.lock:
                d = self.draft
                return Response.json_({
                    "step": d["_step"],
                    "notice": d["_notice"],
                    "data": {k: d[k] for k in RECORD_FIELDS},
                })
        if p == "/api/suggest":
            return self.suggest(req)
        m = re.fullmatch(r"/api/step/(\d)", p)
        if m and req.method == "PUT":
            return self.put_step(int(m.group(1)), req)
        if p == "/api/review":
            return self.review()
        if p == "/api/submit" and req.method == "POST":
            return self.submit()
        m = re.fullmatch(r"/api/quote/(CQ-\d+)", p)
        if m:
            rec = next((r for r in self.submissions if r.get("quote_id") == m.group(1)), None)
            if rec is None:
                return Response.json_({"error": "not found"}, 404)
            return Response.json_({"quote_id": rec["quote_id"], "sections": self._sections(rec)})
        return Response.json_({"error": "not found"}, 404)

    def suggest(self, req: Request) -> Response:
        slow(0.3)
        kind = req.query.get("kind", "")
        q = req.query.get("q", "").strip().casefold()
        out: list[dict[str, str]] = []
        if not q:
            return Response.json_(out)
        if kind == "state":
            for code, name in STATES:
                if name.casefold().startswith(q) or code.casefold() == q:
                    out.append({"label": f"{name} ({code})", "value": code})
        elif kind == "make":
            out = [{"label": mk, "value": mk} for mk in MODELS if mk.casefold().startswith(q)]
        elif kind == "model":
            models = MODELS.get(req.query.get("make", ""), [])
            out = [{"label": md, "value": md} for md in models if q in md.casefold()]
        elif kind == "city":
            out = [
                {"label": f"{c}, {s}", "value": c, "state": s}
                for c, s in CITIES
                if c.casefold().startswith(q.split(",")[0].strip())
            ]
        return Response.json_(out[:8])

    # ------------------------------------------------------------------
    def _validate(self, n: int, data: dict[str, Any]) -> dict[str, str]:
        e: dict[str, str] = {}
        need = "This field is required."
        if n == 1:
            for f in ("first_name", "last_name", "email"):
                if not str(data.get(f, "")).strip():
                    e[f] = need
            if not parse_date(str(data.get("dob", ""))):
                e["dob"] = "Enter a valid date of birth."
            if data.get("license_state") not in STATE_NAME:
                e["license_state"] = "Choose a state from the list."
            if data.get("marital_status") not in MARITAL:
                e["marital_status"] = need
        elif n == 2:
            for i, drv in enumerate(data.get("additional_drivers") or []):
                for f in DRIVER_FIELDS:
                    v = str(drv.get(f, "")).strip()
                    if not v or (f == "dob" and not parse_date(v)) or (f == "relationship" and v not in RELATIONSHIP):
                        e[f"additional_drivers.{i}.{f}"] = need
        elif n == 3:
            if data.get("vehicle_year") not in YEARS:
                e["vehicle_year"] = need
            if data.get("make") not in MODELS:
                e["make"] = "Choose a make from the list."
            elif data.get("model") not in MODELS[data["make"]]:
                e["model"] = "Choose a model from the list."
            if data.get("use") not in USES:
                e["use"] = need
            if data.get("use") == "Commute" and parse_int(data.get("commute_miles")) is None:
                e["commute_miles"] = need
            if data.get("use") == "Business" and data.get("rideshare") not in ("Yes", "No"):
                e["rideshare"] = need
            if parse_int(data.get("annual_miles")) is None:
                e["annual_miles"] = need
            if not re.fullmatch(r"\d{5}", str(data.get("garaging_zip", ""))):
                e["garaging_zip"] = "Enter a 5-digit ZIP code."
            if (data.get("garaging_city"), data.get("garaging_state")) not in CITIES:
                e["garaging_city"] = "Choose a city from the list."
            if data.get("ownership") not in OWNERSHIP:
                e["ownership"] = need
            if data.get("ownership") in ("Finance", "Lease") and not str(data.get("lienholder", "")).strip():
                e["lienholder"] = need
        elif n == 4:
            if data.get("bi_limits") not in BI:
                e["bi_limits"] = need
            for f in ("collision_ded", "comp_ded"):
                if data.get(f) not in DEDS:
                    e[f] = need
        return e

    def _clean(self, n: int, data: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in STEP_FIELDS[n]:
            v = data.get(f, "")
            if f == "additional_drivers":
                out[f] = [
                    {k: (parse_date(str(d.get(k, ""))) if k == "dob" else " ".join(str(d.get(k, "")).split()))
                     for k in DRIVER_FIELDS}
                    for d in (v or [])
                ]
            elif f == "dob":
                out[f] = parse_date(str(v))
            elif f in ("annual_miles", "commute_miles"):
                iv = parse_int(v)
                out[f] = "" if iv is None else iv
            else:
                out[f] = " ".join(str(v).split())
        if n == 3:
            if out.get("use") != "Commute":
                out["commute_miles"] = ""
            if out.get("use") != "Business":
                out["rideshare"] = ""
            if out.get("ownership") == "Own":
                out["lienholder"] = ""
        return out

    def put_step(self, n: int, req: Request) -> Response:
        body = req.json or {}
        data = body.get("data") or {}
        partial = bool(body.get("partial"))
        with self.lock:
            self.draft["_started"] = True
            if not partial and self.fire_once(f"put_step_{n}"):
                for k, v in self.config.get("reset_on_resume", {}).items():
                    self.draft[k] = v
                self.draft["_step"] = n
                self.drop_sessions()
                return Response.json_({"error": "session_expired"}, 401)
            clean = self._clean(n, data)
            if partial:
                self.draft.update(clean)
                return Response.json_({"ok": True})
            errors = self._validate(n, clean)
            if errors:
                return Response.json_({"ok": False, "errors": errors}, 422)
            self.draft.update(clean)
            if n == 1:
                self._maybe_prefill()
            self.draft["_step"] = n + 1
            return Response.json_({"ok": True, "next": n + 1, "notice": self.draft["_notice"]})

    def _maybe_prefill(self) -> None:
        prior = self.config.get("prior_policy")
        if not prior or self.draft["_prefilled"]:
            return
        if (str(self.draft["email"]).casefold() == prior["email"].casefold()
                and self.draft["dob"] == prior["dob"]):
            self.draft["_prefilled"] = True
            self.draft["additional_drivers"] = json.loads(json.dumps(prior["drivers"]))
            self.draft["_notice"] = (
                f"Welcome back. We found your current Cobalt Ridge policy {prior['number']} and copied "
                "the drivers listed on it."
            )

    # ------------------------------------------------------------------
    def _display(self, f: str, v: Any, rec: dict[str, Any]) -> str:
        if v in ("", None):
            return ""
        if f == "dob":
            y, m, d = str(v).split("-")
            return f"{m}/{d}/{y}"
        if f == "license_state":
            return f"{STATE_NAME.get(v, v)} ({v})"
        if f == "garaging_city":
            return f"{v}, {rec.get('garaging_state', '')}"
        if f in ("collision_ded", "comp_ded"):
            return _ded_label(str(v))
        if f == "annual_miles":
            return f"{int(v):,}"
        return str(v)

    def _sections(self, rec: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for i, (title, fields) in enumerate(REVIEW_LABELS):
            rows = [{"label": lab, "value": self._display(f, rec.get(f), rec)} for f, lab in fields
                    if rec.get(f) not in ("", None)]
            out.append({"title": title, "step": [1, 3, 4][i], "rows": rows})
        drivers = [
            {"label": f"Driver {i + 2}",
             "value": f"{d['first_name']} {d['last_name']}, born {self._display('dob', d['dob'], rec)}, {d['relationship']}"}
            for i, d in enumerate(rec.get("additional_drivers") or [])
        ]
        out.insert(1, {"title": "Other drivers", "step": 2, "rows": drivers or [{"label": "Other drivers", "value": "None"}]})
        return out

    def review(self) -> Response:
        with self.lock:
            return Response.json_({"sections": self._sections(self.draft)})

    def submit(self) -> Response:
        with self.lock:
            for n in STEP_FIELDS:
                if self._validate(n, self.draft):
                    return Response.json_({"ok": False, "error": f"Step {n} is incomplete.", "step": n}, 422)
            self.quote_seq += 1
            rec = {f: json.loads(json.dumps(self.draft[f])) for f in RECORD_FIELDS}
            rec["quote_id"] = f"CQ-{730400 + self.quote_seq}"
            self.store_submission(rec)
            return Response.json_({"ok": True, "quote_id": rec["quote_id"]})


_META = json.dumps({
    "marital": MARITAL, "relationship": RELATIONSHIP, "uses": USES, "ownership": OWNERSHIP, "bi": BI,
    "deds": DEDS, "years": YEARS, "states": dict(STATES),
})

SHELL = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Get an auto quote | Cobalt Ridge Insurance</title>
<style>
:root{--ink:#14213d;--muted:#5c6784;--line:#d5dae6;--brand:#1f4fd1;--bg:#f5f7fb;--err:#b42318}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:var(--ink);background:var(--bg)}
header{background:#fff;border-bottom:1px solid var(--line);padding:14px 24px;display:flex;gap:12px;align-items:center}
.logo{width:28px;height:28px;border-radius:7px;background:linear-gradient(135deg,#1f4fd1,#0a1f5c)}
.brand{font-weight:700;letter-spacing:.2px}
main{max-width:720px;margin:28px auto;padding:0 16px}
.progress{display:flex;gap:6px;margin-bottom:18px}.progress div{flex:1;height:5px;border-radius:3px;background:var(--line)}
.progress div.on{background:var(--brand)}
.card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:22px 24px;margin-bottom:16px}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 18px}
.cr-field{margin:0 0 16px}.cr-label{font-weight:600;font-size:14px;margin-bottom:6px}
.cr-input,select.cr-input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:9px;font:inherit;background:#fff}
.cr-error{color:var(--err);font-size:13px;margin-top:4px}
.cr-radios{display:flex;flex-wrap:wrap;gap:8px}
.cr-radio{border:1px solid var(--line);border-radius:999px;padding:8px 14px;cursor:pointer;user-select:none;background:#fff}
.cr-radio[aria-checked=true]{border-color:var(--brand);background:#e9efff;color:#1239a6;font-weight:600}
.cr-listbox{position:absolute;z-index:50;list-style:none;margin:0;padding:4px;background:#fff;border:1px solid var(--line);border-radius:10px;box-shadow:0 8px 24px rgba(20,33,61,.15)}
.cr-listbox li{padding:8px 10px;border-radius:7px;cursor:pointer}.cr-listbox li[aria-selected=true],.cr-listbox li:hover{background:#eef2ff}
.actions{display:flex;justify-content:space-between;margin-top:8px}
button{font:inherit;border-radius:9px;padding:10px 18px;border:1px solid var(--line);background:#fff;cursor:pointer}
button.primary{background:var(--brand);color:#fff;border-color:var(--brand)}
fieldset{border:1px solid var(--line);border-radius:12px;padding:12px 16px;margin:0 0 14px}
legend{font-weight:700;padding:0 6px}
.notice{background:#eef6ff;border:1px solid #c5dcff;border-radius:10px;padding:10px 14px;margin-bottom:16px}
dl{display:grid;grid-template-columns:220px 1fr;gap:6px 12px;margin:0}dt{color:var(--muted)}dd{margin:0;font-weight:600}
.backdrop{position:fixed;inset:0;background:rgba(10,20,40,.45);display:flex;align-items:center;justify-content:center;z-index:100}
.modal{background:#fff;border-radius:14px;padding:22px 24px;width:380px}
.modal label{display:block;font-weight:600;font-size:14px;margin:10px 0 6px}
</style></head><body>
<header><div class="logo" aria-hidden="true"></div><div class="brand">Cobalt Ridge Insurance</div></header>
<main id="app" aria-live="polite"><div class="card">Loading your quote...</div></main>
<script>
const META = __META__;
const S = {step: 1, data: {}, errors: {}, notice: ''};
let uid = 0;
const nid = p => p + '-' + (++uid).toString(36);
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
async function api(method, url, body) {
  const r = await fetch(url, {method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  if (r.status === 401) { showLogin(); throw new Error('auth'); }
  return {status: r.status, body: await r.json()};
}
async function load() {
  const {body} = await api('GET', '/api/draft');
  S.step = body.step; S.data = body.data; S.notice = body.notice || ''; S.errors = {};
  render();
}
function labelDiv(id, text) { const d = el('div', 'cr-label', text); d.id = id; return d; }
function errFor(key) { if (!S.errors[key]) return null; const e = el('div', 'cr-error', S.errors[key]); e.setAttribute('role', 'alert'); return e; }
function textField(key, label, type, get, set) {
  const w = el('div', 'cr-field'); const lid = nid('lbl');
  w.append(labelDiv(lid, label));
  const i = el('input', 'cr-input'); i.type = type || 'text'; i.setAttribute('aria-labelledby', lid);
  i.value = get() == null ? '' : get(); i.addEventListener('input', () => set(i.value));
  w.append(i); const e = errFor(key); if (e) w.append(e); return w;
}
function selectField(key, label, options, get, set) {
  const w = el('div', 'cr-field'); const lid = nid('lbl');
  w.append(labelDiv(lid, label));
  const s = el('select', 'cr-input'); s.setAttribute('aria-labelledby', lid);
  s.append(new Option('Select one', ''));
  options.forEach(([v, t]) => s.append(new Option(t, v)));
  s.value = get() || ''; s.addEventListener('change', () => set(s.value));
  w.append(s); const e = errFor(key); if (e) w.append(e); return w;
}
function radioGroup(key, label, options) {
  const w = el('div', 'cr-field'); const lid = nid('lbl');
  w.append(labelDiv(lid, label));
  const g = el('div', 'cr-radios'); g.setAttribute('role', 'radiogroup'); g.setAttribute('aria-labelledby', lid);
  options.forEach(v => {
    const r = el('div', 'cr-radio', v); r.setAttribute('role', 'radio'); r.tabIndex = 0;
    r.setAttribute('aria-checked', S.data[key] === v ? 'true' : 'false');
    const choose = () => { S.data[key] = v; render(); };
    r.addEventListener('click', choose);
    r.addEventListener('keydown', ev => { if (ev.key === ' ' || ev.key === 'Enter') { ev.preventDefault(); choose(); } });
    g.append(r);
  });
  w.append(g); const e = errFor(key); if (e) w.append(e); return w;
}
function combo(key, label, kind, text, onPick, onClear, extra) {
  const w = el('div', 'cr-field'); const lid = nid('lbl');
  w.append(labelDiv(lid, label));
  const i = el('input', 'cr-input'); const lb = nid('listbox');
  i.setAttribute('role', 'combobox'); i.setAttribute('aria-autocomplete', 'list'); i.setAttribute('aria-expanded', 'false');
  i.setAttribute('aria-controls', lb); i.setAttribute('aria-labelledby', lid); i.autocomplete = 'off';
  i.value = text || '';
  let timer = null, seq = 0, items = [], active = -1;
  const close = () => { const o = document.getElementById(lb); if (o) o.remove(); i.setAttribute('aria-expanded', 'false'); i.removeAttribute('aria-activedescendant'); active = -1; };
  const pick = it => { i.value = it.label; onPick(it); close(); };
  const open = list => {
    close(); items = list;
    const ul = el('ul', 'cr-listbox'); ul.id = lb; ul.setAttribute('role', 'listbox'); ul.setAttribute('aria-labelledby', lid);
    const r = i.getBoundingClientRect();
    ul.style.left = (r.left + window.scrollX) + 'px'; ul.style.top = (r.bottom + window.scrollY + 4) + 'px'; ul.style.width = r.width + 'px';
    if (!list.length) { const li = el('li', null, 'No matches'); li.setAttribute('aria-disabled', 'true'); ul.append(li); }
    list.forEach((it, n) => {
      const li = el('li', null, it.label); li.id = lb + '-' + n; li.setAttribute('role', 'option'); li.setAttribute('aria-selected', 'false');
      li.addEventListener('mousedown', ev => { ev.preventDefault(); pick(it); });
      ul.append(li);
    });
    document.body.append(ul); i.setAttribute('aria-expanded', 'true');
  };
  i.addEventListener('input', () => {
    onClear(); clearTimeout(timer);
    timer = setTimeout(async () => {
      const mine = ++seq; const q = i.value.trim(); if (!q) { close(); return; }
      const params = new URLSearchParams(Object.assign({kind, q}, extra ? extra() : {}));
      const r = await fetch('/api/suggest?' + params.toString());
      if (r.status === 401) { showLogin(); return; }
      const list = await r.json(); if (mine !== seq || document.activeElement !== i) return; open(list);
    }, 150);
  });
  i.addEventListener('keydown', ev => {
    const ul = document.getElementById(lb); if (!ul || !items.length) return;
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault(); active = (active + (ev.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      ul.querySelectorAll('li').forEach((li, n) => li.setAttribute('aria-selected', n === active ? 'true' : 'false'));
      i.setAttribute('aria-activedescendant', lb + '-' + active);
    } else if (ev.key === 'Enter' && active >= 0) { ev.preventDefault(); pick(items[active]); }
    else if (ev.key === 'Escape') close();
  });
  i.addEventListener('blur', () => setTimeout(close, 200));
  w.append(i); const e = errFor(key); if (e) w.append(e); return w;
}
function shell(title, sub) {
  const app = document.getElementById('app'); app.innerHTML = '';
  const p = el('div', 'progress'); for (let n = 1; n <= 5; n++) { const d = el('div', n <= S.step ? 'on' : ''); p.append(d); }
  app.append(p);
  const c = el('section', 'card'); const h = el('h1', null, title); c.append(h); if (sub) c.append(el('p', 'sub', sub));
  if (S.notice && S.step === 2) { const n = el('div', 'notice', S.notice); n.setAttribute('role', 'status'); c.append(n); }
  app.append(c); return c;
}
function actions(card, back, nextLabel, next) {
  const a = el('div', 'actions');
  const b = el('button', null, 'Back'); b.type = 'button'; if (!back) b.style.visibility = 'hidden'; b.addEventListener('click', back || (() => {}));
  const n = el('button', 'primary', nextLabel); n.type = 'button'; n.addEventListener('click', next);
  a.append(b, n); card.append(a);
}
function stepData(n) {
  const keys = {1: ['first_name','last_name','dob','email','license_state','marital_status'], 2: ['additional_drivers'],
    3: ['vehicle_year','make','model','use','commute_miles','rideshare','annual_miles','garaging_zip','garaging_city','garaging_state','ownership','lienholder'],
    4: ['bi_limits','collision_ded','comp_ded']}[n];
  const out = {}; keys.forEach(k => out[k] = S.data[k]); return out;
}
async function save(n) {
  let r;
  try { r = await api('PUT', '/api/step/' + n, {data: stepData(n)}); } catch (e) { return; }
  if (r.status === 422) { S.errors = r.body.errors || {}; render(); return; }
  S.errors = {};
  try { await load(); } catch (e) { return; }
}
async function goBack(n) {
  try { await api('PUT', '/api/step/' + n, {data: stepData(n), partial: true}); } catch (e) { return; }
  S.errors = {}; S.step = n - 1; render();
}
function render() {
  document.querySelectorAll('.cr-listbox').forEach(x => x.remove());
  const d = S.data;
  if (S.step === 1) {
    const c = shell('About you', 'Tell us about the primary driver.');
    c.append(textField('first_name', 'First name', 'text', () => d.first_name, v => d.first_name = v));
    c.append(textField('last_name', 'Last name', 'text', () => d.last_name, v => d.last_name = v));
    c.append(textField('dob', 'Date of birth', 'date', () => d.dob, v => d.dob = v));
    c.append(textField('email', 'Email address', 'email', () => d.email, v => d.email = v));
    const st = d.license_state ? META.states[d.license_state] + ' (' + d.license_state + ')' : '';
    c.append(combo('license_state', 'State where licensed', 'state', st, it => d.license_state = it.value, () => d.license_state = ''));
    c.append(radioGroup('marital_status', 'Marital status', META.marital));
    actions(c, null, 'Continue', () => save(1));
  } else if (S.step === 2) {
    const c = shell('Other drivers', 'List everyone in your household who drives the vehicle.');
    (d.additional_drivers || []).forEach((drv, n) => {
      const fs = el('fieldset'); const lg = el('legend', null, 'Driver ' + (n + 2)); lg.id = nid('legend'); fs.append(lg);
      const mk = (k, label, type) => { const f = textField('additional_drivers.' + n + '.' + k, label, type, () => drv[k], v => drv[k] = v);
        const inp = f.querySelector('input'); inp.setAttribute('aria-labelledby', lg.id + ' ' + inp.getAttribute('aria-labelledby')); return f; };
      fs.append(mk('first_name', 'First name'), mk('last_name', 'Last name'), mk('dob', 'Date of birth', 'date'));
      const sf = selectField('additional_drivers.' + n + '.relationship', 'Relationship to you', META.relationship.map(x => [x, x]), () => drv.relationship, v => drv.relationship = v);
      const sel = sf.querySelector('select'); sel.setAttribute('aria-labelledby', lg.id + ' ' + sel.getAttribute('aria-labelledby'));
      fs.append(sf);
      const rm = el('button', null, 'Remove driver'); rm.type = 'button'; rm.setAttribute('aria-label', 'Remove driver ' + (n + 2));
      rm.addEventListener('click', () => { d.additional_drivers.splice(n, 1); render(); });
      fs.append(rm); c.append(fs);
    });
    const add = el('button', null, '+ Add another driver'); add.type = 'button';
    add.addEventListener('click', () => { (d.additional_drivers = d.additional_drivers || []).push({first_name: '', last_name: '', dob: '', relationship: ''}); render(); });
    c.append(add); c.append(el('div'));
    actions(c, () => goBack(2), 'Continue', () => save(2));
  } else if (S.step === 3) {
    const c = shell('Your vehicle', 'Tell us about the car you want to insure.');
    c.append(selectField('vehicle_year', 'Model year', META.years.map(y => [y, y]), () => d.vehicle_year, v => d.vehicle_year = v));
    c.append(combo('make', 'Make', 'make', d.make, it => { d.make = it.value; d.model = ''; const m = document.querySelector('[data-k=model]'); if (m) m.value = ''; }, () => { d.make = ''; d.model = ''; }));
    const mc = combo('model', 'Model', 'model', d.model, it => d.model = it.value, () => d.model = '', () => ({make: d.make || ''}));
    mc.querySelector('input').dataset.k = 'model'; c.append(mc);
    c.append(radioGroup('use', 'Primary use', META.uses));
    if (d.use === 'Commute') c.append(textField('commute_miles', 'One-way commute (miles)', 'text', () => d.commute_miles, v => d.commute_miles = v));
    if (d.use === 'Business') c.append(radioGroup('rideshare', 'Used for rideshare or delivery?', ['Yes', 'No']));
    c.append(textField('annual_miles', 'Estimated annual mileage', 'text', () => d.annual_miles, v => d.annual_miles = v));
    c.append(textField('garaging_zip', 'Garaging ZIP code', 'text', () => d.garaging_zip, v => d.garaging_zip = v));
    const ct = d.garaging_city ? d.garaging_city + ', ' + d.garaging_state : '';
    c.append(combo('garaging_city', 'Garaging city', 'city', ct, it => { d.garaging_city = it.value; d.garaging_state = it.state; }, () => { d.garaging_city = ''; d.garaging_state = ''; }));
    c.append(radioGroup('ownership', 'Ownership', META.ownership));
    if (d.ownership === 'Finance' || d.ownership === 'Lease') c.append(textField('lienholder', 'Lienholder or lessor name', 'text', () => d.lienholder, v => d.lienholder = v));
    actions(c, () => goBack(3), 'Continue', () => save(3));
  } else if (S.step === 4) {
    const c = shell('Coverage', 'Choose your limits. You can change them later.');
    c.append(radioGroup('bi_limits', 'Bodily injury liability', META.bi));
    const dl = META.deds.map(v => [v, v === 'No coverage' ? v : '$' + Number(v).toLocaleString('en-US')]);
    c.append(selectField('collision_ded', 'Collision deductible', dl, () => d.collision_ded, v => d.collision_ded = v));
    c.append(selectField('comp_ded', 'Comprehensive deductible', dl, () => d.comp_ded, v => d.comp_ded = v));
    actions(c, () => goBack(4), 'Review quote', () => save(4));
  } else if (S.step === 5) {
    renderReview();
  }
}
async function renderReview() {
  let r; try { r = await api('GET', '/api/review'); } catch (e) { return; }
  const c = shell('Review your details', 'Make sure everything is right before we price your quote.');
  r.body.sections.forEach(sec => {
    const h = el('h2', null, sec.title); h.style.fontSize = '16px'; c.append(h);
    const dl = el('dl'); sec.rows.forEach(row => { dl.append(el('dt', null, row.label), el('dd', null, row.value)); }); c.append(dl);
    const ed = el('button', null, 'Edit'); ed.type = 'button'; ed.setAttribute('aria-label', 'Edit ' + sec.title.toLowerCase());
    ed.style.marginTop = '8px'; ed.addEventListener('click', () => { S.step = sec.step; render(); }); c.append(ed);
  });
  if (S.submitError) { const e = el('div', 'cr-error', S.submitError); e.setAttribute('role', 'alert'); c.append(e); }
  actions(c, () => { S.step = 4; render(); }, 'Get my quote', submitQuote);
}
async function submitQuote() {
  let r; try { r = await api('POST', '/api/submit', {}); } catch (e) { return; }
  if (r.status !== 200) { S.submitError = r.body.error; S.step = r.body.step || 5; render(); return; }
  const q = await api('GET', '/api/quote/' + r.body.quote_id);
  const app = document.getElementById('app'); app.innerHTML = '';
  const c = el('section', 'card'); c.append(el('h1', null, 'Your quote is ready'));
  const p = el('p', 'sub'); p.append('Quote number '); const b = el('strong', null, r.body.quote_id); b.id = 'quote-id'; p.append(b); c.append(p);
  q.body.sections.forEach(sec => { c.append(el('h2', null, sec.title)); const dl = el('dl'); sec.rows.forEach(row => dl.append(el('dt', null, row.label), el('dd', null, row.value))); c.append(dl); });
  app.append(c);
}
function showLogin() {
  if (document.getElementById('login-modal')) return;
  const bd = el('div', 'backdrop'); bd.id = 'login-modal';
  const m = el('div', 'modal'); m.setAttribute('role', 'dialog'); m.setAttribute('aria-modal', 'true'); m.setAttribute('aria-labelledby', 'login-title');
  const h = el('h1', null, 'Sign in to continue'); h.id = 'login-title'; m.append(h);
  m.append(el('p', 'sub', 'Your session timed out. Sign in to pick up where you left off.'));
  const l1 = el('label', null, 'Email'); l1.htmlFor = 'login-user'; const u = el('input', 'cr-input'); u.id = 'login-user'; u.type = 'email';
  const l2 = el('label', null, 'Password'); l2.htmlFor = 'login-pass'; const pw = el('input', 'cr-input'); pw.id = 'login-pass'; pw.type = 'password';
  const msg = el('div', 'cr-error'); const go = el('button', 'primary', 'Sign in'); go.type = 'button'; go.style.marginTop = '14px';
  go.addEventListener('click', async () => {
    const r = await fetch('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username: u.value, password: pw.value})});
    if (r.status !== 200) { msg.textContent = (await r.json()).error; return; }
    bd.remove(); load();
  });
  m.append(l1, u, l2, pw, msg, go); bd.append(m); document.body.append(bd);
}
load().catch(() => {});
</script></body></html>""".replace("__META__", _META)

