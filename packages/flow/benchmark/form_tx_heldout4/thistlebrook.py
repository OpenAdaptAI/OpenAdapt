"""Thistlebrook Pet Health: pet insurance quote, one question group per screen.

A consumer flow that shows one card at a time with a progress bar. Choices
are ARIA radio "chips" (buttons with ``role=radio``), the breed is an async
ARIA combobox, and the plan builder recalculates as chips are picked. Every
screen saves through a JSON call. A summary screen lists every answer with a
Change button per screen. If the session ends when the quote is requested,
the card turns into a sign-in form.
"""

from __future__ import annotations

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
    "first_name", "last_name", "email", "phone", "zip",
    "pet_name", "species", "breed", "weight_lb", "birth_month", "birth_year",
    "spayed_neutered", "indoor_only", "recent_illness", "condition_notes",
    "annual_limit", "reimbursement", "deductible", "wellness", "start_date",
]

DOG_BREEDS = [
    "Labrador Retriever", "Labrador Retriever Mix", "Golden Retriever", "Golden Retriever Mix",
    "German Shepherd Dog", "German Shepherd Mix", "Australian Shepherd", "Border Collie", "Beagle", "Boxer",
    "Dachshund", "French Bulldog", "Poodle (Standard)", "Poodle (Miniature)", "Siberian Husky", "Corgi (Pembroke)",
    "Mixed Breed (Small)", "Mixed Breed (Medium)", "Mixed Breed (Large)",
]
CAT_BREEDS = [
    "Domestic Shorthair", "Domestic Medium Hair", "Domestic Longhair", "Maine Coon", "Siamese", "Ragdoll",
    "Bengal", "Mixed Breed Cat",
]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
YEARS = list(range(2026, 2005, -1))
YES_NO = ["Yes", "No"]
LIMITS = ["$5,000", "$10,000", "$20,000", "Unlimited"]
REIMB = ["70%", "80%", "90%"]
DEDUCT = [100, 250, 500, 750]
TODAY = "2026-11-21"  # fixed portal clock
EARLIEST_START = "2026-11-22"

SCREENS: dict[str, list[str]] = {
    "owner": ["first_name", "last_name"],
    "contact": ["email", "phone", "zip"],
    "pet": ["pet_name", "species"],
    "breed": ["breed", "weight_lb", "birth_month", "birth_year"],
    "health": ["spayed_neutered", "indoor_only", "recent_illness", "condition_notes"],
    "plan": ["annual_limit", "reimbursement", "deductible", "wellness"],
    "start": ["start_date"],
}
LABELS = {
    "first_name": "First name", "last_name": "Last name", "email": "Email", "phone": "Mobile number",
    "zip": "ZIP code", "pet_name": "Pet's name", "species": "Species", "breed": "Breed",
    "weight_lb": "Weight (lb)", "birth_month": "Birth month", "birth_year": "Birth year",
    "spayed_neutered": "Spayed or neutered", "indoor_only": "Indoor-only cat",
    "recent_illness": "Illness or injury in the last 12 months", "condition_notes": "Describe the condition",
    "annual_limit": "Annual limit", "reimbursement": "Reimbursement", "deductible": "Annual deductible",
    "wellness": "Add routine care (wellness)", "start_date": "Policy start date",
}
ZIP_STATES = {"0": "MA", "1": "NY", "2": "NC", "3": "TN", "4": "OH", "5": "MN", "6": "IL", "7": "TX", "8": "CO",
              "9": "WA"}


def _us(iso: Any) -> str:
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(iso or ""))
    return f"{m.group(2)}/{m.group(3)}/{m.group(1)}" if m else str(iso or "")


class Thistlebrook(BasePortal):
    portal_id = "thistlebrook"
    cookie = "tb_sid"

    def fresh_draft(self) -> dict[str, Any]:
        d: dict[str, Any] = {f: "" for f in RECORD_FIELDS}
        d.update(annual_limit="$10,000", reimbursement="80%", deductible=250, wellness=False,
                 start_date=EARLIEST_START)
        d.update(self.config.get("prefill", {}))
        return d

    def _clean(self, screen: str, data: dict[str, Any], species: str) -> tuple[dict[str, Any], dict[str, str]]:
        out: dict[str, Any] = {}
        err: dict[str, str] = {}

        def s(f: str) -> str:
            v = data.get(f, "")
            return "" if v is None else str(v).strip()

        if screen == "owner":
            for f in SCREENS[screen]:
                out[f] = s(f)
                if not out[f]:
                    err[f] = "Please fill this in."
        elif screen == "contact":
            out["email"] = s("email")
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]+", out["email"], re.I):
                err["email"] = "Enter a valid email address."
            out["phone"] = s("phone")
            if len(re.sub(r"\D", "", out["phone"])) != 10:
                err["phone"] = "Enter a 10-digit number."
            out["zip"] = s("zip")
            if not re.fullmatch(r"\d{5}", out["zip"]):
                err["zip"] = "Enter a 5-digit ZIP code."
        elif screen == "pet":
            out["pet_name"] = s("pet_name")
            if not out["pet_name"]:
                err["pet_name"] = "What's your pet called?"
            out["species"] = s("species")
            if out["species"] not in ("Dog", "Cat"):
                err["species"] = "Choose dog or cat."
        elif screen == "breed":
            out["breed"] = s("breed")
            if out["breed"] not in (DOG_BREEDS if species == "Dog" else CAT_BREEDS):
                err["breed"] = "Choose a breed from the list."
            w = parse_int(s("weight_lb"))
            out["weight_lb"] = w
            if w is None or not 1 <= w <= 250:
                err["weight_lb"] = "Enter a weight in pounds."
            out["birth_month"] = s("birth_month")
            if out["birth_month"] not in MONTHS:
                err["birth_month"] = "Choose a month."
            y = parse_int(s("birth_year"))
            out["birth_year"] = y
            if y not in YEARS:
                err["birth_year"] = "Choose a year."
        elif screen == "health":
            for f in ("spayed_neutered", "recent_illness"):
                out[f] = s(f)
                if out[f] not in YES_NO:
                    err[f] = "Please answer."
            out["indoor_only"] = s("indoor_only") if species == "Cat" else ""
            if species == "Cat" and out["indoor_only"] not in YES_NO:
                err["indoor_only"] = "Please answer."
            out["condition_notes"] = s("condition_notes") if out["recent_illness"] == "Yes" else ""
            if out["recent_illness"] == "Yes" and not out["condition_notes"]:
                err["condition_notes"] = "Tell us briefly what happened."
        elif screen == "plan":
            out["annual_limit"] = s("annual_limit")
            if out["annual_limit"] not in LIMITS:
                err["annual_limit"] = "Choose a limit."
            out["reimbursement"] = s("reimbursement")
            if out["reimbursement"] not in REIMB:
                err["reimbursement"] = "Choose a reimbursement."
            out["deductible"] = parse_int(s("deductible"))
            if out["deductible"] not in DEDUCT:
                err["deductible"] = "Choose a deductible."
            out["wellness"] = s("wellness").casefold() in ("true", "yes", "1", "on")
        elif screen == "start":
            iso = parse_date(s("start_date"))
            out["start_date"] = iso
            if not iso:
                err["start_date"] = "Use MM/DD/YYYY."
            elif not EARLIEST_START <= iso <= "2027-11-21":
                err["start_date"] = "Choose a date from tomorrow to a year from now."
        return out, err

    def _premium(self, d: dict[str, Any]) -> int:
        m = 24.0 if d.get("species") == "Dog" else 15.0
        m += int(d.get("weight_lb") or 0) * 0.12
        m *= {"$5,000": 0.8, "$10,000": 1.0, "$20,000": 1.15, "Unlimited": 1.4}.get(d.get("annual_limit"), 1)
        m *= {"70%": 0.85, "80%": 1.0, "90%": 1.17}.get(d.get("reimbursement"), 1)
        m *= {100: 1.2, 250: 1.0, 500: 0.86, 750: 0.78}.get(d.get("deductible"), 1)
        if d.get("wellness"):
            m += 14
        return int(round(m * 12))

    def _display(self, f: str, v: Any) -> str:
        if f == "wellness":
            return "Yes" if v else "No"
        if v in ("", None):
            return "—"
        if f == "deductible":
            return money(int(v))
        if f == "start_date":
            return _us(v)
        return str(v)

    def _rows(self, d: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for screen, fields in SCREENS.items():
            for f in fields:
                if f == "indoor_only" and d.get("species") != "Cat":
                    continue
                if f == "condition_notes" and d.get("recent_illness") != "Yes":
                    continue
                rows.append({"screen": screen, "label": LABELS[f], "value": self._display(f, d.get(f))})
        return rows

    # ---- routing -----------------------------------------------------
    def route(self, req: Request) -> Response:
        p = req.path
        if p == "/":
            return Response.redirect("/quote")
        if p == "/signin":
            if req.method == "POST":
                if self.check_login(req.form.get("email", ""), req.form.get("password", "")):
                    return Response.redirect("/quote").set_cookie(self.cookie, self.new_session())
                return Response.html(self._signin_page("That email and password don't match."), 401)
            return Response.html(self._signin_page(""))
        if p == "/api/session" and req.method == "POST":
            b = req.json or {}
            if self.check_login(str(b.get("email", "")), str(b.get("password", ""))):
                return Response.json_({"ok": True}).set_cookie(self.cookie, self.new_session())
            return Response.json_({"ok": False, "error": "That email and password don't match."}, 401)
        if p == "/quote":
            if not self.has_session(req):
                return Response.redirect("/signin")
            return Response.html(APP)
        m = re.fullmatch(r"/quote/done/(TPH-[0-9A-F]+)", p)
        if m:
            if not self.has_session(req):
                return Response.redirect("/signin")
            return Response.html(self._done(m.group(1)))
        if p.startswith("/api/pet/"):
            if not self.has_session(req):
                return Response.json_({"ok": False, "error": "signed_out"}, 401)
            return self._api(req, p[len("/api/pet/"):])
        return Response.html("<h1>Not found</h1>", 404)

    def _api(self, req: Request, what: str) -> Response:
        if what == "state":
            with self.lock:
                d = dict(self.draft)
            d["start_date"] = _us(d["start_date"])
            return Response.json_({"draft": d})
        if what == "breeds":
            q = re.sub(r"\s+", " ", req.query.get("q", "")).strip().casefold()
            pool = CAT_BREEDS if req.query.get("species") == "Cat" else DOG_BREEDS
            slow(0.3)
            return Response.json_({"results": [b for b in pool if len(q) >= 2 and q in b.casefold()][:8]})
        if what == "answers" and req.method == "POST":
            b = req.json or {}
            screen = str(b.get("screen", ""))
            if screen not in SCREENS:
                return Response.json_({"ok": False, "error": "bad screen"}, 400)
            with self.lock:
                species = self.draft.get("species", "")
            if screen == "pet":
                species = str((b.get("answers") or {}).get("species", ""))
            clean, err = self._clean(screen, b.get("answers") or {}, species)
            if err:
                return Response.json_({"ok": False, "errors": err}, 422)
            with self.lock:
                self.draft.update(clean)
            return Response.json_({"ok": True})
        if what == "summary":
            with self.lock:
                d = dict(self.draft)
            return Response.json_({"rows": self._rows(d), "monthly": money(round(self._premium(d) / 12))})
        if what == "quote" and req.method == "POST":
            if self.fire_once("quote"):
                with self.lock:
                    self.drop_sessions()
                    self.draft["start_date"] = EARLIEST_START
                return Response.json_({"ok": False, "error": "signed_out"}, 401)
            with self.lock:
                d = dict(self.draft)
            rec: dict[str, Any] = {}
            for screen, fields in SCREENS.items():
                data = {f: (_us(d[f]) if f == "start_date" else d[f]) for f in fields}
                clean, err = self._clean(screen, data, d.get("species", ""))
                if err:
                    return Response.json_({"ok": False, "error": "Some answers are missing.", "screen": screen}, 422)
                rec.update(clean)
            with self.lock:
                self.quote_seq += 1
                ref = f"TPH-{0x3A71C + self.quote_seq:X}"
            rec["quote_ref"] = ref
            rec["premium"] = self._premium(rec)
            self.store_submission(rec)
            return Response.json_({"ok": True, "quote_ref": ref})
        return Response.json_({"error": "not found"}, 404)

    # ---- pages -------------------------------------------------------
    def _signin_page(self, error: str) -> str:
        err = f'<p class="err" role="alert">{esc(error)}</p>' if error else ""
        return SHELL.replace("{{BODY}}", f"""
<main class="card"><h1>Welcome back</h1><p class="sub">Sign in to get a quote for your pet.</p>{err}
<form method="post" action="/signin">
<label for="email">Email</label><input id="email" name="email" type="email">
<label for="password">Password</label><input id="password" name="password" type="password">
<button class="go" type="submit">Sign in</button></form></main>""")

    def _done(self, ref: str) -> str:
        with self.lock:
            rec = next((r for r in self.submissions if r.get("quote_ref") == ref), None)
        if rec is None:
            return "<h1>Quote not found</h1>"
        rows = "".join(f"<div class='row'><dt>{esc(r['label'])}</dt><dd>{esc(r['value'])}</dd></div>"
                       for r in self._rows(rec))
        return SHELL.replace("{{BODY}}", f"""
<main class="card wide"><h1>{esc(rec['pet_name'])}'s quote is ready</h1>
<p class="sub">Quote reference <strong id="quote-ref">{esc(ref)}</strong></p>
<p class="price"><strong id="premium">{esc(money(rec['premium']))}</strong> per year</p>
<dl class="sum">{rows}</dl></main>""")


SHELL = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Thistlebrook Pet Health</title>
<style>
body{margin:0;background:#fbf6ef;font-family:"Avenir Next",Avenir,"Segoe UI",sans-serif;color:#2f2a26}
.brand{padding:18px 26px;font-weight:700;color:#7a3f6d;font-size:19px}
.brand small{font-weight:400;color:#8b817a;margin-left:6px}
.progress{height:6px;background:#eadfd3;max-width:560px;margin:0 auto;border-radius:3px;overflow:hidden}
.progress span{display:block;height:100%;background:#7a3f6d;transition:width .2s}
.card{background:#fff;max-width:520px;margin:22px auto;border-radius:18px;padding:28px 30px;box-shadow:0 2px 12px #6b4a3a1f}
.card.wide{max-width:640px}
h1{font-size:24px;margin:0 0 6px}.sub{color:#8b817a;margin:0 0 18px}
label,.q{display:block;font-weight:600;margin:16px 0 6px}
input,select,textarea{font:inherit;width:100%;box-sizing:border-box;padding:11px 12px;border:1.5px solid #d8cabd;border-radius:10px;background:#fff}
textarea{min-height:70px}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chips [role=radio]{font:inherit;padding:10px 18px;border-radius:999px;border:1.5px solid #d8cabd;background:#fff;cursor:pointer}
.chips [role=radio][aria-checked=true]{background:#7a3f6d;color:#fff;border-color:#7a3f6d}
.check{display:flex;gap:10px;align-items:center;font-weight:400}.check input{width:auto}
button.go{font:inherit;margin-top:22px;width:100%;padding:13px;border:none;border-radius:12px;background:#7a3f6d;color:#fff;font-weight:600;cursor:pointer}
button.link{background:none;border:none;color:#7a3f6d;cursor:pointer;font:inherit;padding:0;text-decoration:underline}
.err{color:#b3261e;font-size:14px;margin:4px 0 0}
.combo{position:relative}
[role=listbox]{position:absolute;left:0;right:0;z-index:4;background:#fff;border:1.5px solid #d8cabd;border-radius:10px;margin:4px 0 0;padding:4px 0;list-style:none}
[role=option]{padding:9px 12px;cursor:pointer}[role=option]:hover{background:#f4ebf2}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
dl.sum{margin:0}.sum .row{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-bottom:1px solid #f0e6dc}
.sum dt{color:#6f655e}.sum dd{margin:0;font-weight:600;text-align:right}
.group{display:flex;justify-content:space-between;align-items:center;margin-top:16px;color:#7a3f6d;font-weight:700}
.price{font-size:22px}.est{color:#7a3f6d;font-weight:600;margin-top:14px}
.hint{color:#8b817a;font-size:14px;margin:6px 0 0}
</style></head><body><div class="brand">Thistlebrook <small>Pet Health</small></div>{{BODY}}</body></html>"""

APP = SHELL.replace("{{BODY}}", r"""
<div class="progress" aria-hidden="true"><span id="bar" style="width:0"></span></div>
<main class="card" id="card" aria-live="polite"></main>
<script>
const MONTHS = __MONTHS__, YEARS = __YEARS__;
const ORDER = ['owner','contact','pet','breed','health','plan','start','summary'];
let d = {}, screen = 'owner', editing = false;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function chips(field, label, options, value){
  return `<span class="q" id="q-${field}">${label}</span><div class="chips" role="radiogroup" aria-labelledby="q-${field}" data-field="${field}">` +
    options.map(o => `<button type="button" role="radio" data-value="${esc(o[0])}" aria-checked="${String(o[0]) === String(value)}">${esc(o[1])}</button>`).join('') +
    `</div><p class="err" data-err="${field}"></p>`;
}
function text(field, label, value, extra){
  return `<label for="${field}">${label}</label><input id="${field}" value="${esc(value)}" ${extra||''}><p class="err" data-err="${field}"></p>`;
}
const STATES = __ZIPS__;
function render(){
  const i = ORDER.indexOf(screen);
  $('bar').style.width = Math.round(100 * i / (ORDER.length - 1)) + '%';
  const name = d.pet_name || 'your pet';
  let h = '';
  if (screen === 'owner') h = `<h1>First, what's your name?</h1><p class="sub">It takes about two minutes.</p>` + text('first_name','First name',d.first_name) + text('last_name','Last name',d.last_name);
  if (screen === 'contact') h = `<h1>How can we reach you?</h1><p class="sub">We'll send your quote here.</p>` + text('email','Email',d.email,'type="email"') + text('phone','Mobile number',d.phone,'type="tel"') + text('zip','ZIP code',d.zip,'inputmode="numeric" maxlength="5"') + `<p class="hint" id="zip-hint"></p>`;
  if (screen === 'pet') h = `<h1>Tell us about your pet</h1>` + text('pet_name',"Pet's name",d.pet_name) + chips('species','Species',[['Dog','Dog'],['Cat','Cat']],d.species);
  if (screen === 'breed') h = `<h1>What breed is ${esc(name)}?</h1><div class="combo"><label for="breed">Breed</label><input id="breed" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="breed-list" autocomplete="off" value="${esc(d.breed)}"><ul id="breed-list" role="listbox" aria-label="Breeds" hidden></ul><p class="err" data-err="breed"></p></div>` +
    text('weight_lb','Weight (lb)',d.weight_lb ?? '','inputmode="numeric"') +
    `<div class="row2"><div><label for="birth_month">Birth month</label><select id="birth_month"><option value="">Month</option>${MONTHS.map(m => `<option${m===d.birth_month?' selected':''}>${m}</option>`).join('')}</select><p class="err" data-err="birth_month"></p></div>` +
    `<div><label for="birth_year">Birth year</label><select id="birth_year"><option value="">Year</option>${YEARS.map(y => `<option${String(y)===String(d.birth_year)?' selected':''}>${y}</option>`).join('')}</select><p class="err" data-err="birth_year"></p></div></div>`;
  if (screen === 'health') {
    h = `<h1>A few questions about ${esc(name)}'s health</h1>` + chips('spayed_neutered','Spayed or neutered',[['Yes','Yes'],['No','No']],d.spayed_neutered);
    if (d.species === 'Cat') h += chips('indoor_only','Indoor-only cat',[['Yes','Yes'],['No','No']],d.indoor_only) + `<p class="hint">Indoor-only cats qualify for a lower rate.</p>`;
    h += chips('recent_illness','Illness or injury in the last 12 months',[['Yes','Yes'],['No','No']],d.recent_illness);
    h += `<div id="notes-wrap" ${d.recent_illness==='Yes'?'':'hidden'}><label for="condition_notes">Describe the condition</label><textarea id="condition_notes">${esc(d.condition_notes)}</textarea><p class="err" data-err="condition_notes"></p></div>`;
  }
  if (screen === 'plan') h = `<h1>Build ${esc(name)}'s plan</h1><p class="sub">We picked our most popular plan. Change anything you like.</p>` +
    chips('annual_limit','Annual limit',[['$5,000','$5,000'],['$10,000','$10,000'],['$20,000','$20,000'],['Unlimited','Unlimited']],d.annual_limit) +
    chips('reimbursement','Reimbursement',[['70%','70%'],['80%','80%'],['90%','90%']],d.reimbursement) +
    chips('deductible','Annual deductible',[['100','$100'],['250','$250'],['500','$500'],['750','$750']],d.deductible) +
    `<label class="check"><input type="checkbox" id="wellness" ${d.wellness?'checked':''}> Add routine care (wellness)</label>`;
  if (screen === 'start') h = `<h1>When should coverage start?</h1><p class="sub">The earliest start is tomorrow.</p>` + text('start_date','Policy start date',d.start_date,'placeholder="MM/DD/YYYY"');
  if (screen === 'summary') { h = `<h1>Check ${esc(name)}'s quote</h1><p class="sub">Make sure everything is right before you get your price.</p><div id="sum">Loading…</div><p class="est" id="monthly"></p><p class="err" id="quote-err"></p><button class="go" type="button" id="get-quote">Get my quote</button>`; }
  if (screen === 'signin') h = `<h1>Please sign in again</h1><p class="sub">You were signed out. Sign in to see your quote.</p>` + text('rs-email','Email','','type="email"') + `<label for="rs-pass">Password</label><input id="rs-pass" type="password"><p class="err" id="rs-err"></p><button class="go" type="button" id="rs-go">Sign in</button>`;
  if (!['summary','signin'].includes(screen)) h += `<button class="go" type="button" id="next">${editing ? 'Save and return to summary' : 'Next'}</button>` + (i > 0 && !editing ? `<p><button class="link" type="button" id="back">Back</button></p>` : '');
  $('card').innerHTML = h;
  $('card').dataset.screen = screen;
  wire();
}
function chipValue(field){ const b = document.querySelector(`[data-field="${field}"] [aria-checked=true]`); return b ? b.dataset.value : ''; }
function wire(){
  document.querySelectorAll('.chips').forEach(g => g.querySelectorAll('[role=radio]').forEach(b => b.onclick = () => {
    g.querySelectorAll('[role=radio]').forEach(x => x.setAttribute('aria-checked','false'));
    b.setAttribute('aria-checked','true');
    const field = g.dataset.field;
    if (field === 'recent_illness') $('notes-wrap').hidden = b.dataset.value !== 'Yes';
    if (field === 'deductible' && chipValue('annual_limit') === 'Unlimited') {
      // Unlimited plans re-price from the default reimbursement when the deductible changes.
      document.querySelectorAll('[data-field="reimbursement"] [role=radio]').forEach(x => x.setAttribute('aria-checked', String(x.dataset.value === '80%')));
    }
  }));
  if ($('next')) $('next').onclick = next;
  if ($('back')) $('back').onclick = () => { screen = ORDER[ORDER.indexOf(screen) - 1]; render(); };
  if ($('zip')) { const f = () => { const z = $('zip').value; $('zip-hint').textContent = z.length === 5 ? `We cover pets in ${STATES[z[0]] || 'your state'}.` : ''; }; $('zip').addEventListener('input', f); f(); }
  if ($('breed')) wireBreed();
  if (screen === 'summary') loadSummary();
  if ($('rs-go')) $('rs-go').onclick = async () => {
    const r = await fetch('/api/session', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({email: $('rs-email').value, password: $('rs-pass').value})});
    if (!r.ok) { $('rs-err').textContent = "That email and password don't match."; return; }
    await load(); screen = 'summary'; render();
  };
}
let bseq = 0, btimer = null, chosenBreed = null;
function wireBreed(){
  chosenBreed = d.breed || null;
  const box = $('breed'), list = $('breed-list');
  box.addEventListener('input', () => {
    chosenBreed = null; clearTimeout(btimer);
    btimer = setTimeout(async () => {
      const my = ++bseq;
      const r = await fetch('/api/pet/breeds?species=' + encodeURIComponent(d.species || '') + '&q=' + encodeURIComponent(box.value));
      if (r.status === 401) { screen = 'signin'; render(); return; }
      const j = await r.json(); if (my !== bseq) return;
      list.innerHTML = '';
      j.results.forEach(n => { const li = document.createElement('li'); li.setAttribute('role','option'); li.textContent = n;
        li.onmousedown = (e) => { e.preventDefault(); box.value = n; chosenBreed = n; list.hidden = true; box.setAttribute('aria-expanded','false'); };
        list.appendChild(li); });
      list.hidden = !j.results.length; box.setAttribute('aria-expanded', String(!!j.results.length));
    }, 180);
  });
}
function answers(){
  const a = {};
  if (screen === 'owner') { a.first_name = $('first_name').value; a.last_name = $('last_name').value; }
  if (screen === 'contact') { a.email = $('email').value; a.phone = $('phone').value; a.zip = $('zip').value; }
  if (screen === 'pet') { a.pet_name = $('pet_name').value; a.species = chipValue('species'); }
  if (screen === 'breed') { a.breed = chosenBreed || ''; a.weight_lb = $('weight_lb').value; a.birth_month = $('birth_month').value; a.birth_year = $('birth_year').value; }
  if (screen === 'health') { a.spayed_neutered = chipValue('spayed_neutered'); a.indoor_only = d.species === 'Cat' ? chipValue('indoor_only') : ''; a.recent_illness = chipValue('recent_illness'); a.condition_notes = $('condition_notes').value; }
  if (screen === 'plan') { a.annual_limit = chipValue('annual_limit'); a.reimbursement = chipValue('reimbursement'); a.deductible = chipValue('deductible'); a.wellness = $('wellness').checked; }
  if (screen === 'start') a.start_date = $('start_date').value;
  return a;
}
async function next(){
  document.querySelectorAll('[data-err]').forEach(e => e.textContent = '');
  const r = await fetch('/api/pet/answers', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({screen, answers: answers()})});
  if (r.status === 401) { screen = 'signin'; render(); return; }
  const j = await r.json();
  if (!j.ok) { for (const [k,v] of Object.entries(j.errors || {})) { const e = document.querySelector(`[data-err="${k}"]`); if (e) e.textContent = v; } return; }
  await load();
  screen = editing ? 'summary' : ORDER[ORDER.indexOf(screen) + 1];
  if (screen === 'summary') editing = false;
  render();
}
async function loadSummary(){
  const r = await fetch('/api/pet/summary');
  if (r.status === 401) { screen = 'signin'; render(); return; }
  const j = await r.json();
  const names = {owner:'You', contact:'Contact', pet:'Your pet', breed:'Breed and age', health:'Health', plan:'Plan', start:'Start date'};
  let h = '';
  for (const s of Object.keys(names)) {
    const rows = j.rows.filter(x => x.screen === s); if (!rows.length) continue;
    h += `<div class="group">${names[s]} <button class="link" type="button" data-change="${s}">Change</button></div><dl class="sum">` +
      rows.map(x => `<div class="row"><dt>${esc(x.label)}</dt><dd>${esc(x.value)}</dd></div>`).join('') + '</dl>';
  }
  $('sum').innerHTML = h; $('monthly').textContent = 'About ' + j.monthly + ' a month';
  document.querySelectorAll('[data-change]').forEach(b => b.onclick = () => { editing = true; screen = b.dataset.change; render(); });
  $('get-quote').onclick = async () => {
    $('quote-err').textContent = '';
    const q = await fetch('/api/pet/quote', {method:'POST'});
    if (q.status === 401) { screen = 'signin'; render(); return; }
    const k = await q.json();
    if (!k.ok) { $('quote-err').textContent = k.error; return; }
    location.href = '/quote/done/' + k.quote_ref;
  };
}
async function load(){ const r = await fetch('/api/pet/state'); if (r.status === 401) { screen = 'signin'; return; } d = (await r.json()).draft; }
load().then(render);
</script>""").replace("__MONTHS__", repr(MONTHS).replace("'", '"')).replace("__YEARS__", repr(YEARS)).replace(
    "__ZIPS__", repr(ZIP_STATES).replace("'", '"'))
