"""Ferncastle Indemnity Producer Desk: rental-dwelling (landlord) quote.

A multi-tab agency workstation. One page holds a tab strip; every tab is
rendered in script from a field schema and saved to the server with
``PUT /api/draft/<tab>``. Tabs can be opened in any order. The Insured tab
has a client-lookup dialog that copies a client file into the quote. The
Rate tab reads ``GET /api/rate`` and shows the summary and premium; an
expired session is handled by an in-page sign-in dialog.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from .common import BasePortal, Request, Response, esc, parse_date, parse_int

STATES = ["CT", "DE", "MD", "NJ", "NY", "PA", "VA"]
CONSTRUCTION = ["Frame", "Joisted masonry", "Masonry noncombustible", "Fire resistive"]
OCCUPANCY = ["Owner occupies one unit", "All units rented to tenants", "One or more units vacant"]
RENTS = ["None", "6 months", "12 months"]
LIABILITY = [("100000", "$100,000"), ("300000", "$300,000"), ("500000", "$500,000")]
DEDUCTIBLE = [("500", "$500"), ("1000", "$1,000"), ("2500", "$2,500")]

# tab id, title, fields: (name, label, kind, options, required)
TABS: list[tuple[str, str, list[tuple[str, str, str, Any, bool]]]] = [
    ("insured", "Insured", [
        ("client_no", "Client number", "readonly", None, False),
        ("first_name", "First name", "text", None, True),
        ("last_name", "Last name", "text", None, True),
        ("email", "Email", "email", None, True),
        ("phone", "Phone", "tel", None, True),
    ]),
    ("location", "Location", [
        ("street", "Street address", "text", None, True),
        ("city", "City", "text", None, True),
        ("state", "State", "select", [(s, s) for s in STATES], True),
        ("zip", "ZIP code", "text", None, True),
    ]),
    ("building", "Building", [
        ("year_built", "Year built", "number", None, True),
        ("units", "Number of units", "select", [(str(n), str(n)) for n in range(1, 5)], True),
        ("construction", "Construction", "select", [(c, c) for c in CONSTRUCTION], True),
        ("sqft", "Total square feet", "number", None, True),
        ("roof_year", "Roof replaced (year)", "number", None, True),
        ("mortgagee", "Mortgagee name (blank if none)", "text", None, False),
    ]),
    ("rental", "Rental", [
        ("monthly_rent", "Monthly rent per unit ($)", "number", None, True),
        ("occupancy", "Unit occupancy", "select", [(o, o) for o in OCCUPANCY], True),
    ]),
    ("coverage", "Coverage", [
        ("eff_date", "Policy effective date", "date", None, True),
        ("dwelling_limit", "Dwelling limit ($)", "number", None, True),
        ("loss_of_rents", "Loss of rents", "select", [(r, r) for r in RENTS], True),
        ("liability", "Premises liability", "select", LIABILITY, True),
        ("deductible", "Deductible", "select", DEDUCTIBLE, True),
    ]),
]
INT_FIELDS = {"year_built", "units", "sqft", "roof_year", "monthly_rent", "dwelling_limit"}
RECORD_FIELDS = [name for _t, _title, fs in TABS for name, *_ in fs]
FIELD = {name: (label, kind, opts, req) for _t, _title, fs in TABS for name, label, kind, opts, req in fs}


def _occupancy_applies(d: dict[str, Any]) -> bool:
    return (parse_int(d.get("units")) or 0) >= 2


CSS = """
:root{--ink:#1d2b24;--line:#c5cec8;--brand:#2f5d46;--soft:#eef3ef}
*{box-sizing:border-box}body{margin:0;font:14px/1.4 "Segoe UI",Roboto,Arial,sans-serif;color:var(--ink);background:#f6f7f5}
header{background:var(--brand);color:#fff;padding:10px 18px;display:flex;justify-content:space-between;align-items:center}
header b{font-size:17px;letter-spacing:.3px}header span{font-size:12px;opacity:.85}
main{display:grid;grid-template-columns:190px 1fr;gap:0;min-height:calc(100vh - 44px)}
nav[role=tablist]{background:#e4eae6;border-right:1px solid var(--line);padding:10px 0}
nav button{display:block;width:100%;text-align:left;border:0;background:none;padding:9px 16px;font:inherit;cursor:pointer;color:var(--ink)}
nav button[aria-selected=true]{background:#fff;border-left:4px solid var(--brand);font-weight:600}
nav button .ok{color:#2f7d4f;font-size:11px;margin-left:6px}
#panel{padding:18px 26px;max-width:760px}
#panel h2{margin:0 0 14px;font-size:18px}
.fc-row{display:grid;grid-template-columns:240px 1fr;align-items:center;margin:0 0 10px}
.fc-row label{font-weight:600;font-size:13px}
.fc-row input,.fc-row select{font:inherit;padding:6px 8px;border:1px solid #9aa79f;border-radius:3px;width:280px;background:#fff}
.fc-row input[readonly]{background:#eef0ee;color:#555}
.fc-row.fc-err input,.fc-row.fc-err select{border-color:#b3261e;background:#fff5f4}
.fc-row .msg{grid-column:2;color:#b3261e;font-size:12px}
.fc-actions{margin-top:18px;display:flex;gap:10px}
button.pri{background:var(--brand);color:#fff;border:0;padding:8px 16px;border-radius:3px;font:inherit;cursor:pointer}
button.sec{background:#fff;border:1px solid #9aa79f;padding:7px 14px;border-radius:3px;font:inherit;cursor:pointer}
table.sum{border-collapse:collapse;min-width:520px}table.sum td,table.sum th{padding:5px 10px;border-bottom:1px solid var(--line);text-align:left}
table.sum th{background:var(--soft);font-size:12px;text-transform:uppercase;letter-spacing:.4px}
.prem{font-size:20px;font-weight:700;margin:14px 0}
dialog{border:1px solid #889;border-radius:6px;padding:18px 22px;min-width:420px}
dialog::backdrop{background:rgba(20,30,25,.45)}
.banner{background:#fff6dc;border:1px solid #e2c766;padding:8px 12px;margin:0 0 14px;border-radius:3px}
"""

APP_JS = r"""
const TABS = __TABS__;
let DRAFT = {};
let current = null;
const panel = () => document.getElementById('panel');

function api(method, url, body) {
  return fetch(url, {method, headers: {'Content-Type': 'application/json'},
                     body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin'})
    .then(r => r.json().then(j => ({status: r.status, body: j})));
}

function needsReLogin(retry) {
  const d = document.getElementById('relogin');
  document.getElementById('rl-msg').textContent = 'Your session timed out. Sign in to continue where you left off.';
  d.dataset.retry = retry;
  if (!d.open) d.showModal();
}

function shows(f) {
  if (f[0] === 'occupancy') return parseInt(DRAFT.units || '0', 10) >= 2;
  return true;
}

function control(f) {
  const [name, label, kind, opts] = f;
  const v = DRAFT[name] === undefined || DRAFT[name] === null ? '' : String(DRAFT[name]);
  const id = 'f-' + name;
  if (kind === 'select') {
    let h = `<select id="${id}" name="${name}"><option value="">Select...</option>`;
    for (const [ov, ot] of opts) h += `<option value="${ov}"${ov === v ? ' selected' : ''}>${ot}</option>`;
    return h + '</select>';
  }
  if (kind === 'readonly') return `<input id="${id}" name="${name}" readonly value="${v.replace(/"/g, '&quot;')}">`;
  return `<input id="${id}" name="${name}" type="${kind}" value="${v.replace(/"/g, '&quot;')}">`;
}

function renderTab(id, errors) {
  current = id;
  for (const b of document.querySelectorAll('nav [role=tab]')) b.setAttribute('aria-selected', b.dataset.tab === id ? 'true' : 'false');
  const p = panel();
  p.setAttribute('aria-labelledby', 'tab-' + id);
  if (id === 'rate') return loadRate();
  const tab = TABS.find(t => t[0] === id);
  let h = `<h2>${tab[1]}</h2>`;
  if (id === 'insured') h += `<p><button type="button" class="sec" id="find-client">Find existing client</button></p>`;
  if (DRAFT._copied_from && (id === 'location' || id === 'building' || id === 'rental'))
    h += `<div class="banner">Details on this tab were copied from client file ${DRAFT._copied_from}.</div>`;
  for (const f of tab[2]) {
    if (!shows(f)) continue;
    const err = errors && errors.includes(f[0]);
    h += `<div class="fc-row${err ? ' fc-err' : ''}" data-field="${f[0]}"><label for="f-${f[0]}">${f[1]}</label>${control(f)}` +
         (err ? `<span class="msg">Required</span>` : '') + `</div>`;
  }
  h += `<div class="fc-actions"><button type="button" class="pri" id="save">Save and continue</button></div>`;
  p.innerHTML = h;
  p.dataset.tab = id;
  document.getElementById('save').onclick = () => save(id);
  const fc = document.getElementById('find-client');
  if (fc) fc.onclick = () => { document.getElementById('lk-result').innerHTML = ''; document.getElementById('lookup').showModal(); };
}

function save(id) {
  const vals = {};
  for (const el of panel().querySelectorAll('input,select')) vals[el.name] = el.value;
  panel().dataset.busy = '1';
  api('PUT', '/api/draft/' + id, vals).then(r => {
    delete panel().dataset.busy;
    if (r.status === 401) return needsReLogin('save:' + id);
    DRAFT = r.body.draft;
    markDone();
    if (r.status === 422) return renderTab(id, r.body.errors);
    const order = TABS.map(t => t[0]).concat(['rate']);
    renderTab(order[order.indexOf(id) + 1]);
  });
}

function markDone() {
  for (const b of document.querySelectorAll('nav [role=tab]')) {
    const done = (DRAFT._saved || []).includes(b.dataset.tab);
    b.querySelector('.ok').textContent = done ? 'saved' : '';
  }
}

function fmt(name, v) {
  if (v === '' || v === null || v === undefined) return '';
  for (const t of TABS) for (const f of t[2]) if (f[0] === name && f[2] === 'select') {
    const o = f[3].find(o => o[0] === String(v)); return o ? o[1] : String(v);
  }
  return String(v);
}

function loadRate() {
  const p = panel();
  p.dataset.tab = 'rate-loading';
  p.innerHTML = '<h2>Rate</h2><p>Rating...</p>';
  api('GET', '/api/rate').then(r => {
    if (r.status === 401) return needsReLogin('rate');
    DRAFT = r.body.draft;
    let h = '<h2>Rate</h2>';
    if (r.body.missing.length) {
      h += '<div class="banner">Complete these tabs before rating: ' + r.body.missing.join(', ') + '</div>';
      p.innerHTML = h; p.dataset.tab = 'rate-incomplete'; return;
    }
    h += '<table class="sum" id="rate-summary"><tr><th>Item</th><th>Value</th></tr>';
    for (const [label, name] of r.body.rows) h += `<tr><td>${label}</td><td>${fmt(name, DRAFT[name])}</td></tr>`;
    h += `</table><div class="prem">Estimated annual premium: $${r.body.premium.toLocaleString('en-US')}</div>`;
    h += '<div class="fc-actions"><button type="button" class="pri" id="request">Request quote</button></div>';
    p.innerHTML = h; p.dataset.tab = 'rate';
    document.getElementById('request').onclick = submitQuote;
  });
}

function submitQuote() {
  api('POST', '/api/submit', {}).then(r => {
    if (r.status === 401) return needsReLogin('rate');
    if (r.status !== 200) { loadRate(); return; }
    const q = r.body;
    let h = `<h2>Quote issued</h2><p>Quote number <b id="quote-no">${q.quote_no}</b>. These are the details we stored:</p>`;
    h += '<table class="sum" id="stored"><tr><th>Item</th><th>Value</th></tr>';
    for (const [label, name] of q.rows) h += `<tr><td>${label}</td><td>${fmt(name, q.record[name])}</td></tr>`;
    h += '</table>';
    panel().innerHTML = h; panel().dataset.tab = 'done';
  });
}

function boot() {
  for (const b of document.querySelectorAll('nav [role=tab]')) b.onclick = () => renderTab(b.dataset.tab);
  document.getElementById('lk-search').onclick = () => {
    const n = document.getElementById('lk-number').value.trim();
    api('GET', '/api/client?number=' + encodeURIComponent(n)).then(r => {
      const box = document.getElementById('lk-result');
      if (r.status === 401) { document.getElementById('lookup').close(); return needsReLogin('none'); }
      if (!r.body.found) { box.innerHTML = '<p class="msg" id="lk-none">No client found with that number.</p>'; return; }
      const c = r.body.client;
      box.innerHTML = `<div class="banner" id="lk-match"><b>${c.number}</b> ${c.name}<br>${c.location}</div>` +
        '<button type="button" class="pri" id="lk-apply">Copy client details to this quote</button>';
      document.getElementById('lk-apply').onclick = () => api('POST', '/api/client/apply', {number: c.number}).then(r2 => {
        DRAFT = r2.body.draft; document.getElementById('lookup').close(); markDone(); renderTab('insured');
      });
    });
  };
  document.getElementById('lk-cancel').onclick = () => document.getElementById('lookup').close();
  document.getElementById('rl-go').onclick = () => {
    const d = document.getElementById('relogin');
    api('POST', '/api/login', {username: document.getElementById('rl-user').value,
                               password: document.getElementById('rl-pass').value}).then(r => {
      if (r.status !== 200) { document.getElementById('rl-msg').textContent = 'Sign-in failed. Check the producer code and password.'; return; }
      d.close();
      const retry = d.dataset.retry || '';
      api('GET', '/api/draft').then(r2 => {
        DRAFT = r2.body.draft; markDone();
        if (retry.startsWith('save:')) renderTab(retry.slice(5)); else if (retry === 'rate') renderTab('rate'); else renderTab(current || 'insured');
      });
    });
  };
  api('GET', '/api/draft').then(r => { DRAFT = r.body.draft; markDone(); renderTab('insured'); });
}
document.addEventListener('DOMContentLoaded', boot);
"""


def _shell(title: str, body: str, script: str = "") -> str:
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{esc(title)} | Ferncastle Indemnity Producer Desk</title><style>{CSS}</style></head><body>"
        "<header><b>FERNCASTLE INDEMNITY</b><span>Producer Desk &middot; Rental Dwelling DP-3</span></header>"
        f"{body}{script}</body></html>"
    )


class Ferncastle(BasePortal):
    portal_id = "ferncastle"
    cookie = "FCDESK"

    def fresh_draft(self) -> dict[str, Any]:
        d: dict[str, Any] = {name: "" for name in RECORD_FIELDS}
        d.update(liability="300000", deductible="1000", loss_of_rents="None")
        d["_saved"] = []
        d["_copied_from"] = ""
        return d

    # ------------------------------------------------------------------
    def route(self, req: Request) -> Response:
        p = req.path
        if p in ("/", "/desk"):
            return Response.redirect("/desk/quote")
        if p == "/desk/login":
            return self.login_page(req)
        if p == "/api/login" and req.method == "POST":
            body = req.json or {}
            if self.check_login(str(body.get("username", "")), str(body.get("password", ""))):
                return Response.json_({"ok": True}).set_cookie(self.cookie, self.new_session())
            return Response.json_({"ok": False}, 403)
        if p.startswith("/api/"):
            if not self.has_session(req):
                return Response.json_({"error": "session"}, 401)
            return self.api(req)
        if not self.has_session(req):
            return Response.redirect("/desk/login?next=" + quote(p))
        if p == "/desk/quote":
            return self.desk()
        return Response.html(_shell("Not found", "<p style='padding:20px'>Not found.</p>"), 404)

    def login_page(self, req: Request) -> Response:
        nxt = req.query.get("next") or req.form.get("next") or "/desk/quote"
        if not nxt.startswith("/"):
            nxt = "/desk/quote"
        msg = ""
        if req.method == "POST":
            if self.check_login(req.form.get("producer", ""), req.form.get("password", "")):
                return Response.redirect(nxt).set_cookie(self.cookie, self.new_session())
            msg = "<p role='alert' style='color:#b3261e'>That producer code and password do not match.</p>"
        body = f"""<div style="max-width:380px;margin:60px auto;background:#fff;border:1px solid #c5cec8;padding:22px 26px">
<h2 style="margin-top:0">Producer sign in</h2>{msg}
<form method="post" action="/desk/login"><input type="hidden" name="next" value="{esc(nxt)}">
<div class="fc-row" style="grid-template-columns:1fr"><label for="producer">Producer code</label><input id="producer" name="producer"></div>
<div class="fc-row" style="grid-template-columns:1fr"><label for="password">Password</label><input id="password" name="password" type="password"></div>
<button class="pri" type="submit">Sign in</button></form></div>"""
        return Response.html(_shell("Sign in", body))

    def desk(self) -> Response:
        tabs = "".join(
            f"<button role='tab' id='tab-{tid}' data-tab='{tid}' aria-controls='panel' aria-selected='false'>"
            f"{esc(title)}<span class='ok'></span></button>"
            for tid, title, _f in TABS
        ) + "<button role='tab' id='tab-rate' data-tab='rate' aria-controls='panel' aria-selected='false'>Rate<span class='ok'></span></button>"
        dialogs = """
<dialog id="lookup" aria-labelledby="lk-title"><h3 id="lk-title" style="margin-top:0">Find existing client</h3>
<div class="fc-row"><label for="lk-number">Client number</label><input id="lk-number"></div>
<div class="fc-actions"><button type="button" class="pri" id="lk-search">Search</button><button type="button" class="sec" id="lk-cancel">Cancel</button></div>
<div id="lk-result" aria-live="polite"></div></dialog>
<dialog id="relogin" aria-labelledby="rl-title"><h3 id="rl-title" style="margin-top:0">Sign in again</h3><p id="rl-msg"></p>
<div class="fc-row"><label for="rl-user">Producer code</label><input id="rl-user"></div>
<div class="fc-row"><label for="rl-pass">Password</label><input id="rl-pass" type="password"></div>
<div class="fc-actions"><button type="button" class="pri" id="rl-go">Sign in</button></div></dialog>"""
        schema = json.dumps([[tid, title, [list(f[:4]) for f in fs]] for tid, title, fs in TABS])
        body = f"<main><nav role='tablist' aria-label='Quote sections' aria-orientation='vertical'>{tabs}</nav>" \
               f"<section id='panel' role='tabpanel' aria-live='polite'></section></main>{dialogs}"
        return Response.html(_shell("New quote", body, "<script>" + APP_JS.replace("__TABS__", schema) + "</script>"))

    # ------------------------------------------------------------------
    def _public(self) -> dict[str, Any]:
        return {k: v for k, v in self.draft.items()}

    def _missing(self) -> list[str]:
        out = []
        for tid, title, fs in TABS:
            for name, _l, _k, _o, req in fs:
                if not req or (name == "occupancy" and not _occupancy_applies(self.draft)):
                    continue
                if self.draft.get(name, "") == "":
                    out.append(title)
                    break
        return out

    def _rows(self) -> list[tuple[str, str]]:
        rows = []
        for _tid, _title, fs in TABS:
            for name, label, *_ in fs:
                if name == "occupancy" and not _occupancy_applies(self.draft):
                    continue
                if name == "client_no" and not self.draft.get("client_no"):
                    continue
                rows.append((label, name))
        return rows

    def _premium(self) -> int:
        d = self.draft
        base = (parse_int(d.get("dwelling_limit")) or 0) * 0.0047
        base *= {"500": 1.08, "1000": 1.0, "2500": 0.9}.get(str(d.get("deductible")), 1.0)
        base += {"100000": 60, "300000": 95, "500000": 130}.get(str(d.get("liability")), 0)
        base += {"None": 0, "6 months": 70, "12 months": 120}.get(str(d.get("loss_of_rents")), 0)
        base *= 1 + 0.06 * ((parse_int(d.get("units")) or 1) - 1)
        return int(round(base))

    def api(self, req: Request) -> Response:
        p = req.path
        if p == "/api/draft" and req.method == "GET":
            return Response.json_({"draft": self._public()})
        if p.startswith("/api/draft/") and req.method == "PUT":
            return self.save_tab(p.rsplit("/", 1)[1], req.json or {})
        if p == "/api/client" and req.method == "GET":
            client = self.config.get("client_file")
            n = req.query.get("number", "").strip().upper()
            if not client or client["number"].upper() != n:
                return Response.json_({"found": False})
            f = client["fields"]
            return Response.json_({"found": True, "client": {
                "number": client["number"], "name": f"{f['first_name']} {f['last_name']}",
                "location": f"{f['street']}, {f['city']}, {f['state']} {f['zip']}"}})
        if p == "/api/client/apply" and req.method == "POST":
            client = self.config.get("client_file")
            n = str((req.json or {}).get("number", "")).strip().upper()
            with self.lock:
                if client and client["number"].upper() == n:
                    for k, v in client["fields"].items():
                        self.draft[k] = v
                    self.draft["client_no"] = client["number"]
                    self.draft["_copied_from"] = client["number"]
            return Response.json_({"draft": self._public()})
        if p == "/api/rate" and req.method == "GET":
            with self.lock:
                if self.fire_once("open_rate"):
                    for k, v in self.config.get("reset_on_resume", {}).items():
                        self.draft[k] = v
                    self.drop_sessions()
                    return Response.json_({"error": "session"}, 401)
                missing = self._missing()
                return Response.json_({"draft": self._public(), "missing": missing, "rows": self._rows(),
                                       "premium": 0 if missing else self._premium()})
        if p == "/api/submit" and req.method == "POST":
            with self.lock:
                if self._missing():
                    return Response.json_({"error": "incomplete"}, 422)
                self.quote_seq += 1
                record = {k: self.draft.get(k, "") for k in RECORD_FIELDS}
                record["quote_no"] = f"FC-Q-{58300 + self.quote_seq}"
                self.store_submission(record)
                return Response.json_({"quote_no": record["quote_no"], "record": record, "rows": self._rows()})
        return Response.json_({"error": "not found"}, 404)

    def save_tab(self, tab: str, vals: dict[str, Any]) -> Response:
        spec = next((t for t in TABS if t[0] == tab), None)
        if spec is None:
            return Response.json_({"error": "unknown tab"}, 404)
        with self.lock:
            for name, _label, kind, opts, _req in spec[2]:
                if name not in vals or kind == "readonly":
                    continue
                raw = " ".join(str(vals[name]).split())
                if kind == "date":
                    self.draft[name] = parse_date(raw)
                elif name in INT_FIELDS:
                    iv = parse_int(raw)
                    self.draft[name] = "" if iv is None else iv
                elif kind == "select":
                    self.draft[name] = raw if raw in [o for o, _t in opts] else ""
                else:
                    self.draft[name] = raw
            if tab == "building" and not _occupancy_applies(self.draft):
                self.draft["occupancy"] = ""
            errors = [
                name for name, _l, _k, _o, req in spec[2]
                if req and self.draft.get(name, "") == ""
                and not (name == "occupancy" and not _occupancy_applies(self.draft))
            ]
            if not errors and tab not in self.draft["_saved"]:
                self.draft["_saved"].append(tab)
            return Response.json_({"draft": self._public(), "errors": errors}, 422 if errors else 200)
