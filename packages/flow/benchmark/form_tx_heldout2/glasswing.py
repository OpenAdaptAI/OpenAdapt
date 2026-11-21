"""Glasswing Home & Renters: an embedded renters-quote widget.

A property manager's resident site (Otterbrook Commons Resident Center)
embeds the carrier's widget in an iframe. The widget is one custom element
(``<gw-quote>``) whose whole UI lives in an open shadow root. On load the
widget asks the host page for the resident's lease details over
``postMessage`` and pre-fills from them. The address field is an ARIA
combobox fed by an asynchronous address service; the belongings step has a
calculator in a modal dialog. The widget keeps its state in script and
posts the finished quote to ``/gw/api/quote``.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from .common import BasePortal, Request, Response, esc, parse_date, parse_int, slow
from .estimators import GLASSWING_ROOM_RATE

STATES = ["CO", "IL", "MN", "OR", "WA", "WI"]
DEDUCTIBLES = ["250", "500", "1000"]
LIABILITY = ["100000", "300000", "500000"]
RECORD_FIELDS = [
    "first_name", "last_name", "dob", "email", "street", "unit", "city", "state", "zip", "start_date",
    "sprinklered", "roommate", "personal_property", "deductible", "liability",
]
LABELS = {
    "first_name": "First name", "last_name": "Last name", "dob": "Date of birth", "email": "Email",
    "street": "Street address", "unit": "Unit", "city": "City", "state": "State", "zip": "ZIP",
    "start_date": "Policy start date", "sprinklered": "Does your building have a fire sprinkler system?",
    "roommate": "Roommate to add to the policy (full name)", "personal_property": "Personal property coverage ($)",
    "deductible": "Deductible", "liability": "Liability limit",
}

HOST_CSS = """
body{margin:0;font:15px/1.45 Georgia,"Times New Roman",serif;background:#f3efe7;color:#2b2520}
.top{background:#3b2f2a;color:#f3e9dc;padding:14px 24px;font-size:20px}
.wrap{display:grid;grid-template-columns:220px 1fr;gap:24px;padding:20px 24px}
.menu a{display:block;padding:8px 10px;color:#3b2f2a;text-decoration:none;border-radius:4px}
.menu a.on{background:#e3d8c8;font-weight:bold}
iframe{width:100%;height:calc(100vh - 96px);min-height:480px;border:1px solid #cdbfa9;border-radius:8px;background:#fff;display:block}
.top{height:22px}.wrap{padding:12px 24px}.menu p{font-size:13px;color:#5a4d44;margin:14px 10px}
"""

WIDGET_JS = r"""
const RATE = __RATE__;
const LABELS = __LABELS__;
const STEPS = ['About you', 'Your rental', 'Your belongings', 'Review'];
const CSS = `:host{display:block;font:15px/1.5 "Helvetica Neue",Arial,sans-serif;color:#17202a}
.card{max-width:620px;margin:0 auto;padding:22px 26px}
.brand{display:flex;align-items:center;gap:8px;font-weight:700;color:#0f5c73;margin-bottom:6px}
.brand i{width:18px;height:18px;border-radius:50% 50% 50% 0;background:linear-gradient(135deg,#7fd0e0,#0f5c73);display:inline-block}
ol.prog{display:flex;gap:6px;list-style:none;padding:0;margin:0 0 18px}
ol.prog li{flex:1;height:5px;border-radius:3px;background:#d7e4e8}ol.prog li.on{background:#0f5c73}
h2{font-size:22px;margin:0 0 16px}
.field{margin:0 0 15px;position:relative}
.field>label,.field>.q{display:block;font-weight:600;font-size:14px;margin-bottom:5px}
.field input,.field select{font:inherit;padding:9px 11px;border:1px solid #9bb3bb;border-radius:6px;width:100%;box-sizing:border-box;background:#fff}
.field input[readonly]{background:#f1f5f6;color:#4a5a60}
.field .hint{font-size:12px;color:#5b6b70;margin-top:3px}
.field.bad input,.field.bad select,.field.bad [role=radiogroup]{outline:2px solid #c0392b}
.field .err{color:#c0392b;font-size:13px}
.row2{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:10px}
[role=radiogroup]{display:flex;gap:16px;padding:4px 2px}
[role=radiogroup] label{font-weight:400}
ul[role=listbox]{position:absolute;z-index:5;left:0;right:0;margin:2px 0 0;padding:4px 0;list-style:none;background:#fff;border:1px solid #9bb3bb;border-radius:6px;box-shadow:0 6px 18px rgba(0,0,0,.12)}
ul[role=listbox] li{padding:8px 12px;cursor:pointer}ul[role=listbox] li[aria-selected=true],ul[role=listbox] li:hover{background:#e6f3f6}
.btns{display:flex;gap:10px;margin-top:22px}
button{font:inherit;border-radius:22px;padding:9px 20px;cursor:pointer;border:1px solid #0f5c73;background:#fff;color:#0f5c73}
button.go{background:#0f5c73;color:#fff}
dialog{border:0;border-radius:12px;padding:22px 26px;width:440px;box-shadow:0 10px 40px rgba(0,0,0,.3)}
dialog::backdrop{background:rgba(10,30,40,.5)}
output{display:block;font-size:24px;font-weight:700;margin:10px 0}
dl.sum{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;margin:0}dl.sum dt{color:#5b6b70}dl.sum dd{margin:0;font-weight:600}
.ref{background:#e6f3f6;border-radius:8px;padding:12px 14px;margin-bottom:14px}`;

function money(n) { return '$' + Number(n).toLocaleString('en-US'); }
function round100(x) { return Math.round(x / 100) * 100; }

class GwQuote extends HTMLElement {
  constructor() {
    super();
    this.root = this.attachShadow({mode: 'open'});
    this.s = {step: 0, first_name: '', last_name: '', dob: '', email: '', addr: null, street_text: '',
              start_date: '', sprinklered: '', roommate: '', personal_property: '', deductible: '500', liability: '300000'};
    this.calc = {rooms: '', furnished: '', valuables: '', base: null, total: null};
    this.errors = [];
    this.timer = null;
  }
  connectedCallback() {
    window.addEventListener('message', ev => {
      if (!ev.data || ev.data.type !== 'gw:prefill') return;
      const r = ev.data.resident || {};
      for (const k of ['first_name', 'last_name', 'email', 'start_date', 'roommate']) if (r[k]) this.s[k] = r[k];
      if (r.address) { this.s.addr = r.address; this.s.street_text = r.address.street; }
      document.documentElement.dataset.ready = '1';
      this.render();
    });
    this.render();
    if (window.parent !== window) window.parent.postMessage({type: 'gw:ready'}, '*');
    else document.documentElement.dataset.ready = '1';
  }
  field(name, inner, hint) {
    const bad = this.errors.includes(name);
    return `<div class="field${bad ? ' bad' : ''}" data-name="${name}">${inner}${hint ? `<div class="hint">${hint}</div>` : ''}` +
           (bad ? `<div class="err" role="alert">Please complete this.</div>` : '') + `</div>`;
  }
  text(name, type) {
    const v = this.s[name] || '';
    return this.field(name, `<label for="in-${name}">${LABELS[name]}</label><input id="in-${name}" name="${name}" type="${type || 'text'}" value="${String(v).replace(/"/g, '&quot;')}">`);
  }
  render() {
    const s = this.s; let h = `<style>${CSS}</style><div class="card"><div class="brand"><i></i>Glasswing Home &amp; Renters</div>`;
    h += '<ol class="prog">' + STEPS.map((t, i) => `<li class="${i <= s.step ? 'on' : ''}" aria-label="${t}"></li>`).join('') + '</ol>';
    if (s.step === 4) { this.root.innerHTML = h + this.doneHtml + '</div>'; this.root.host.dataset.step = 'done'; return; }
    h += `<h2>${STEPS[s.step]}</h2><form novalidate>`;
    if (s.step === 0) {
      h += this.text('first_name') + this.text('last_name') + this.text('dob', 'date') + this.text('email', 'email');
    } else if (s.step === 1) {
      const a = s.addr || {};
      h += this.field('street', `<label for="in-street" id="lb-street">${LABELS.street}</label>
        <input id="in-street" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="addr-list"
               autocomplete="off" value="${(s.street_text || '').replace(/"/g, '&quot;')}">
        <ul id="addr-list" role="listbox" aria-labelledby="lb-street" hidden></ul>`, 'Start typing, then choose your address from the list.');
      h += '<div class="row2">' + ['unit', 'city', 'state', 'zip'].map(k =>
        `<div class="field"><label for="ro-${k}">${LABELS[k]}</label><input id="ro-${k}" readonly value="${a[k] || ''}"></div>`).join('') + '</div>';
      h += this.text('start_date', 'date');
      h += this.field('sprinklered', `<div class="q" id="q-spr">${LABELS.sprinklered}</div><div role="radiogroup" aria-labelledby="q-spr">` +
        ['Yes', 'No'].map(o => `<label><input type="radio" name="sprinklered" value="${o}"${s.sprinklered === o ? ' checked' : ''}> ${o}</label>`).join('') + '</div>');
      h += this.field('roommate', `<label for="in-roommate">${LABELS.roommate}</label><input id="in-roommate" name="roommate" value="${(s.roommate || '').replace(/"/g, '&quot;')}">`,
        'Leave blank if you live alone or your roommate has their own policy.');
    } else if (s.step === 2) {
      h += this.field('personal_property', `<label for="in-personal_property">${LABELS.personal_property}</label>
        <input id="in-personal_property" name="personal_property" type="number" min="5000" step="100" value="${s.personal_property}">
        <button type="button" id="open-calc" style="margin-top:8px">Help me estimate</button>`, 'What it would cost to replace everything you own.');
      h += this.field('deductible', `<div class="q" id="q-ded">${LABELS.deductible}</div><div role="radiogroup" aria-labelledby="q-ded">` +
        __DEDS__.map(o => `<label><input type="radio" name="deductible" value="${o}"${s.deductible === o ? ' checked' : ''}> ${money(o)}</label>`).join('') + '</div>');
      h += this.field('liability', `<label for="in-liability">${LABELS.liability}</label><select id="in-liability" name="liability">` +
        __LIABS__.map(o => `<option value="${o}"${s.liability === o ? ' selected' : ''}>${money(o)}</option>`).join('') + '</select>');
      h += `<dialog id="calc" aria-labelledby="calc-title"><h3 id="calc-title" style="margin-top:0">Belongings calculator</h3>
        <div class="field"><label for="c-rooms">Number of rooms</label><select id="c-rooms"><option value="">Choose...</option>` +
        [1, 2, 3, 4, 5, 6, 7, 8].map(n => `<option value="${n}">${n}</option>`).join('') + `</select><div class="hint">Count bedrooms, living areas and the kitchen.</div></div>
        <div class="field"><label for="c-furn">How furnished are they?</label><select id="c-furn"><option value="">Choose...</option>` +
        Object.keys(RATE).map(k => `<option value="${k}">${k}</option>`).join('') + `</select></div>
        <div class="field"><label for="c-val">Electronics, jewelry and instruments ($)</label><input id="c-val" type="number" min="0" step="100" value="0"></div>
        <button type="button" id="c-calc">Calculate</button><output id="calc-total" aria-live="polite"></output>
        <div class="btns"><button type="button" class="go" id="c-use" disabled>Use this amount</button><button type="button" id="c-close">Close</button></div></dialog>`;
    } else if (s.step === 3) {
      h += '<dl class="sum" id="review">' + this.summaryRows(this.payload()).join('') + '</dl>';
    }
    h += '<div class="btns">' + (s.step > 0 ? '<button type="button" id="back">Back</button>' : '') +
         `<button type="button" class="go" id="next">${s.step === 3 ? 'Get my price' : 'Continue'}</button></div></form></div>`;
    this.root.innerHTML = h;
    this.root.host.dataset.step = String(s.step);
    this.wire();
  }
  summaryRows(p) {
    const show = (k, v) => {
      if (v === '' || v === null || v === undefined) return '&mdash;';
      if (k === 'personal_property' || k === 'deductible' || k === 'liability') return money(v);
      return String(v);
    };
    return __FIELDS__.map(k => `<dt>${LABELS[k]}</dt><dd data-k="${k}">${show(k, p[k])}</dd>`);
  }
  payload() {
    const s = this.s, a = s.addr || {};
    return {first_name: s.first_name, last_name: s.last_name, dob: s.dob, email: s.email, street: a.street || '',
            unit: a.unit || '', city: a.city || '', state: a.state || '', zip: a.zip || '', start_date: s.start_date,
            sprinklered: s.sprinklered, roommate: s.roommate, personal_property: s.personal_property,
            deductible: s.deductible, liability: s.liability};
  }
  collect() {
    const r = this.root;
    for (const el of r.querySelectorAll('form input[name], form select[name]')) {
      if (el.type === 'radio') { if (el.checked) this.s[el.name] = el.value; } else this.s[el.name] = el.value.trim();
    }
  }
  validate() {
    const need = [['first_name', 'last_name', 'dob', 'email'], ['street', 'start_date', 'sprinklered'],
                  ['personal_property', 'deductible', 'liability'], []][this.s.step];
    this.errors = need.filter(k => k === 'street' ? !this.s.addr : !this.s[k]);
    return !this.errors.length;
  }
  wire() {
    const r = this.root;
    const nx = r.getElementById('next'), bk = r.getElementById('back');
    if (bk) bk.onclick = () => { this.collect(); this.errors = []; this.s.step--; this.render(); };
    nx.onclick = () => {
      this.collect();
      if (this.s.step === 3) return this.submit();
      if (!this.validate()) return this.render();
      this.s.step++; this.render();
    };
    const combo = r.getElementById('in-street');
    if (combo) {
      const list = r.getElementById('addr-list');
      combo.addEventListener('input', () => {
        this.s.street_text = combo.value; this.s.addr = null;
        for (const k of ['unit', 'city', 'state', 'zip']) r.getElementById('ro-' + k).value = '';
        clearTimeout(this.timer);
        list.hidden = true; list.innerHTML = ''; combo.setAttribute('aria-expanded', 'false');
        const q = combo.value.trim();
        if (q.length < 3) return;
        this.timer = setTimeout(() => {
          fetch('/gw/api/address?q=' + encodeURIComponent(q), {credentials: 'same-origin'}).then(x => x.json()).then(rows => {
            if (combo.value.trim() !== q) return;
            list.innerHTML = rows.map((a, i) => `<li role="option" id="addr-opt-${i}" aria-selected="false">${a.label}</li>`).join('');
            list.hidden = !rows.length; combo.setAttribute('aria-expanded', rows.length ? 'true' : 'false');
            list.querySelectorAll('li').forEach((li, i) => li.onclick = () => {
              const a = rows[i]; this.s.addr = {street: a.street, unit: a.unit, city: a.city, state: a.state, zip: a.zip};
              this.s.street_text = a.street; combo.value = a.street;
              for (const k of ['unit', 'city', 'state', 'zip']) r.getElementById('ro-' + k).value = a[k] || '';
              list.hidden = true; combo.setAttribute('aria-expanded', 'false');
            });
          });
        }, 200);
      });
    }
    const oc = r.getElementById('open-calc');
    if (oc) {
      const d = r.getElementById('calc');
      oc.onclick = () => { this.collect(); d.showModal(); };
      r.getElementById('c-close').onclick = () => d.close();
      r.getElementById('c-calc').onclick = () => {
        const rooms = parseInt(r.getElementById('c-rooms').value, 10), furn = r.getElementById('c-furn').value;
        const val = parseInt(r.getElementById('c-val').value || '0', 10) || 0;
        if (!rooms || !RATE[furn]) { r.getElementById('calc-total').textContent = 'Choose rooms and furnishing first.'; return; }
        this.calc.base = round100(rooms * RATE[furn]);
        this.calc.total = round100(rooms * RATE[furn] + val);
        r.getElementById('calc-total').textContent = 'Estimated value: ' + money(this.calc.total);
        r.getElementById('c-use').disabled = false;
      };
      r.getElementById('c-use').onclick = () => {
        this.s.personal_property = String(this.calc.base);
        r.getElementById('in-personal_property').value = this.s.personal_property;
        d.close();
      };
    }
  }
  submit() {
    fetch('/gw/api/quote', {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify(this.payload())})
      .then(x => x.json().then(j => ({status: x.status, body: j}))).then(res => {
        if (res.status !== 200) { this.s.step = 0; this.errors = res.body.missing || []; this.render(); return; }
        const q = res.body;
        this.doneHtml = `<h2>Your renters quote</h2><div class="ref">Quote reference <b id="quote-ref">${q.quote_ref}</b><br>` +
          `Monthly price ${money(q.monthly)}</div><dl class="sum" id="stored">${this.summaryRows(q.record).join('')}</dl>`;
        this.s.step = 4; this.render();
      });
  }
}
customElements.define('gw-quote', GwQuote);
"""


def _addr_label(a: dict[str, str]) -> str:
    unit = f" {a['unit']}" if a.get("unit") else ""
    return f"{a['street']}{unit}, {a['city']}, {a['state']} {a['zip']}"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().casefold()


class Glasswing(BasePortal):
    portal_id = "glasswing"
    cookie = "otterbrook_sid"

    def route(self, req: Request) -> Response:
        p = req.path
        if p == "/":
            return Response.redirect("/resident")
        if p == "/signin":
            return self.signin(req)
        if not self.has_session(req):
            if p.startswith("/gw/api/"):
                return Response.json_({"error": "session"}, 401)
            return Response.redirect("/signin?next=" + quote(p))
        if p == "/resident":
            return self.host_page()
        if p == "/gw/embed":
            return self.widget()
        if p == "/gw/api/address":
            return self.addresses(req.query.get("q", ""))
        if p == "/gw/api/quote" and req.method == "POST":
            return self.quote(req.json or {})
        return Response.html("<p>Not found</p>", 404)

    def signin(self, req: Request) -> Response:
        nxt = req.query.get("next") or req.form.get("next") or "/resident"
        if not nxt.startswith("/"):
            nxt = "/resident"
        msg = ""
        if req.method == "POST":
            if self.check_login(req.form.get("email", ""), req.form.get("password", "")):
                return Response.redirect(nxt).set_cookie(self.cookie, self.new_session())
            msg = "<p role='alert' style='color:#a3331f'>We couldn't sign you in with that email and password.</p>"
        body = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Sign in | Otterbrook Commons Resident Center</title>
<style>{HOST_CSS} form{{max-width:360px;margin:50px auto;background:#fff;padding:24px;border-radius:8px;border:1px solid #cdbfa9}}
label{{display:block;margin:12px 0 4px;font-weight:bold}}input{{width:100%;padding:8px;font:inherit;box-sizing:border-box}}</style></head>
<body><div class="top">Otterbrook Commons &middot; Resident Center</div>
<form method="post" action="/signin"><h2 style="margin-top:0">Resident sign in</h2>{msg}<input type="hidden" name="next" value="{esc(nxt)}">
<label for="email">Email</label><input id="email" name="email" type="email">
<label for="password">Password</label><input id="password" name="password" type="password">
<p><button type="submit">Sign in</button></p></form></body></html>"""
        return Response.html(body)

    def host_page(self) -> Response:
        lease = self.config.get("lease", {})
        resident = json.dumps(lease)
        body = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Renters insurance | Otterbrook Commons Resident Center</title>
<style>{HOST_CSS}</style></head><body><div class="top">Otterbrook Commons &middot; Resident Center</div>
<div class="wrap"><nav class="menu" aria-label="Resident menu"><a href="#">Pay rent</a><a href="#">Maintenance requests</a>
<a href="#">Lease documents</a><a class="on" href="/resident" aria-current="page">Renters insurance</a>
<p>Your lease requires renters insurance. Get a quote from our partner Glasswing Home &amp; Renters; we share your lease details with it so you type less.</p></nav>
<main><iframe id="gw-frame" title="Glasswing renters quote" src="/gw/embed"></iframe></main></div>
<script>
const RESIDENT = {resident};
window.addEventListener('message', ev => {{
  if (ev.data && ev.data.type === 'gw:ready')
    document.getElementById('gw-frame').contentWindow.postMessage({{type: 'gw:prefill', resident: RESIDENT}}, '*');
}});
</script></body></html>"""
        return Response.html(body)

    def widget(self) -> Response:
        js = (WIDGET_JS.replace("__RATE__", json.dumps(GLASSWING_ROOM_RATE))
              .replace("__LABELS__", json.dumps(LABELS))
              .replace("__DEDS__", json.dumps(DEDUCTIBLES))
              .replace("__LIABS__", json.dumps(LIABILITY))
              .replace("__FIELDS__", json.dumps(RECORD_FIELDS)))
        body = ("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>Glasswing renters quote</title>"
                "<style>body{margin:0;background:#fff}</style></head><body><gw-quote></gw-quote>"
                f"<script>{js}</script></body></html>")
        return Response.html(body)

    def addresses(self, q: str) -> Response:
        slow(0.25)
        nq = _norm(q)
        rows = []
        for a in self.config.get("addresses", []):
            full = _norm(_addr_label(a))
            if nq and (full.startswith(nq) or _norm(a["street"]).startswith(nq)):
                rows.append(dict(a, label=_addr_label(a)))
        return Response.json_(rows[:8])

    def quote(self, body: dict[str, Any]) -> Response:
        rec: dict[str, Any] = {}
        for k in RECORD_FIELDS:
            rec[k] = " ".join(str(body.get(k, "") or "").split())
        rec["dob"] = parse_date(rec["dob"])
        rec["start_date"] = parse_date(rec["start_date"])
        pp = parse_int(rec["personal_property"])
        rec["personal_property"] = "" if pp is None else pp
        if rec["state"] not in STATES:
            rec["state"] = ""
        if rec["deductible"] not in DEDUCTIBLES:
            rec["deductible"] = ""
        if rec["liability"] not in LIABILITY:
            rec["liability"] = ""
        if rec["sprinklered"] not in ("Yes", "No"):
            rec["sprinklered"] = ""
        optional = {"unit", "roommate"}
        missing = [k for k in RECORD_FIELDS if k not in optional and rec[k] == ""]
        if missing:
            return Response.json_({"missing": missing}, 422)
        with self.lock:
            self.quote_seq += 1
            rec["quote_ref"] = f"GW-R-{71400 + self.quote_seq}"
            stored = self.store_submission(rec)
        monthly = round((pp or 0) * 0.00042 + {"250": 7, "500": 5, "1000": 3}[rec["deductible"]]
                        + {"100000": 2, "300000": 3, "500000": 4}[rec["liability"]])
        return Response.json_({"quote_ref": stored["quote_ref"], "record": rec, "monthly": monthly})
