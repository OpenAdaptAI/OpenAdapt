"""Velloway Insurance: personal umbrella quote, an accessible one-page form.

Server-rendered with progressive enhancement, in the style of public-sector
design systems: one long page of fieldsets, day/month/year date inputs, an
error summary on failed validation, an ARIA 1.2 occupation combobox fed by
an asynchronous service, and a repeatable watercraft group. A household
account number pre-fills the form. A read-only quote summary with a
confirmation checkbox comes before the quote request.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from urllib.parse import quote

from .common import BasePortal, Request, Response, esc, parse_int, slow

STATES = ["GA", "KY", "NC", "OH", "SC", "TN"]
OCCUPATIONS = [
    "Teacher aide", "Teacher", "Registered nurse", "Nurse practitioner", "Software tester", "Software developer",
    "Accountant", "Paralegal", "Attorney", "Electrician", "Electrical engineer", "Retired", "Real estate agent",
    "Physician", "Physician assistant", "Pilot", "Self-employed", "Homemaker", "Farmer", "Firefighter",
    "Pharmacist", "Pharmacy technician", "Architect", "Dental hygienist", "Dentist",
]
WATERCRAFT = ["Personal watercraft", "Sailboat", "Powerboat"]
AUTO = ["250/500", "500/500", "500 CSL"]
HOME = [("300000", "$300,000"), ("500000", "$500,000")]
UMBRELLA = [(str(n * 1000000), f"${n},000,000") for n in (1, 2, 3, 4, 5)]
YN = ["Yes", "No"]
RECORD_FIELDS = [
    "first_name", "last_name", "dob", "email", "phone", "street", "city", "state", "zip", "occupation",
    "drivers", "youthful_drivers", "youthful_violations", "vehicles", "watercraft", "auto_liability",
    "home_liability", "rental_properties", "nonprofit_board", "umbrella_limit", "start_date",
]
LABELS = {
    "first_name": "First name", "last_name": "Last name", "dob": "Date of birth", "email": "Email address",
    "phone": "Phone number", "street": "Address line 1", "city": "Town or city", "state": "State",
    "zip": "ZIP code", "occupation": "Occupation", "drivers": "Licensed drivers in your household",
    "youthful_drivers": "How many of them are under 25?",
    "youthful_violations": "Has any driver under 25 had a moving violation in the last 3 years?",
    "vehicles": "Vehicles your household owns or leases", "watercraft": "Watercraft",
    "auto_liability": "Auto liability limits", "home_liability": "Home liability limit",
    "rental_properties": "Rental properties you own", "nonprofit_board":
        "Does anyone in your household serve as a director or officer of a nonprofit organization?",
    "umbrella_limit": "Umbrella limit", "start_date": "Policy start date",
}
INT_FIELDS = {"drivers", "youthful_drivers", "vehicles", "rental_properties"}

CSS = """
:root{--text:#0b0c0c;--sec:#505a5f;--brand:#5b2a86;--focus:#ffdd00;--err:#d4351c;--line:#b1b4b6}
*{box-sizing:border-box}body{margin:0;font:19px/1.32 Arial,"Helvetica Neue",sans-serif;color:var(--text);background:#fff}
a{color:#1d70b8}header{background:var(--brand);color:#fff;padding:12px 30px;font-weight:700;font-size:22px}
header small{font-weight:400;font-size:15px;margin-left:12px;opacity:.9}
main{max-width:760px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:34px;margin:10px 0 24px}h2{font-size:24px;margin:30px 0 12px}
fieldset{border:0;margin:0 0 26px;padding:0}legend{font-weight:700;font-size:22px;margin-bottom:10px}
.fg{margin:0 0 20px;position:relative}.fg>label,.fg>.q,.fg>fieldset>legend.q{display:block;font-weight:700;font-size:19px;margin-bottom:6px}
.hint{color:var(--sec);font-size:17px;margin:-2px 0 8px}
input[type=text],input[type=email],input[type=tel],input[type=number],select,input[role=combobox]{font:inherit;padding:6px 8px;border:2px solid var(--text);border-radius:0;width:100%;max-width:440px}
input.w2{width:64px}input.w4{width:96px}input:focus,select:focus{outline:3px solid var(--focus);outline-offset:0}
.dates{display:flex;gap:16px}.dates label{display:block;font-weight:400;font-size:17px}
.radios label{display:block;margin:6px 0;font-weight:400}.radios input{transform:scale(1.4);margin-right:10px}
.fg.bad{border-left:5px solid var(--err);padding-left:15px}.fg .emsg{color:var(--err);font-weight:700;display:block;margin-bottom:6px}
.esum{border:5px solid var(--err);padding:16px 20px;margin-bottom:28px}.esum h2{margin-top:0}
.banner{border:5px solid #1d70b8;padding:14px 18px;margin-bottom:26px}
button,.btn{font:inherit;background:#00703c;color:#fff;border:0;box-shadow:0 2px 0 #002d18;padding:9px 16px;cursor:pointer}
button.secondary{background:#f3f2f1;color:var(--text);box-shadow:0 2px 0 #929191}
.wc{border:1px solid var(--line);padding:14px 16px;margin-bottom:14px}.wc legend{font-size:19px}
ul[role=listbox]{position:absolute;z-index:3;left:0;max-width:440px;width:100%;margin:0;padding:0;list-style:none;background:#fff;border:2px solid var(--text);border-top:0}
ul[role=listbox] li{padding:8px 10px;cursor:pointer}ul[role=listbox] li:hover{background:#1d70b8;color:#fff}
dl.sl{margin:0 0 26px}dl.sl div{display:grid;grid-template-columns:42% 1fr auto;gap:10px;border-bottom:1px solid var(--line);padding:10px 0}
dl.sl dt{font-weight:700}dl.sl dd{margin:0}
.panel{background:#00703c;color:#fff;padding:26px;text-align:center;margin-bottom:26px}.panel b{font-size:30px;display:block}
"""


def _page(title: str, body: str, script: str = "") -> str:
    return (f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>{esc(title)} - Velloway Insurance</title>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'><style>{CSS}</style></head><body>"
            "<a href='#main' style='position:absolute;left:-999px'>Skip to main content</a>"
            "<header>Velloway Insurance<small>Personal umbrella</small></header>"
            f"<main id='main'>{body}</main>{script}</body></html>")


def _iso(day: str, month: str, year: str) -> str:
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except (TypeError, ValueError):
        return ""


FORM_JS = r"""
<script>
(function(){
  const combo = document.getElementById('occupation-input'), list = document.getElementById('occupation-list');
  const hidden = document.getElementById('occupation');
  let timer = null;
  combo.addEventListener('input', () => {
    hidden.value = '';
    clearTimeout(timer); list.hidden = true; list.innerHTML = ''; combo.setAttribute('aria-expanded', 'false');
    const q = combo.value.trim(); if (q.length < 2) return;
    timer = setTimeout(() => fetch('/umbrella/api/occupations?q=' + encodeURIComponent(q)).then(r => r.json()).then(rows => {
      if (combo.value.trim() !== q) return;
      list.innerHTML = rows.map((o, i) => `<li role="option" id="occ-${i}" aria-selected="false">${o}</li>`).join('');
      list.hidden = !rows.length; combo.setAttribute('aria-expanded', rows.length ? 'true' : 'false');
      list.querySelectorAll('li').forEach(li => li.onclick = () => {
        combo.value = li.textContent; hidden.value = li.textContent; list.hidden = true; combo.setAttribute('aria-expanded', 'false');
      });
    }), 250);
  });
  const yd = document.getElementById('youthful_drivers'), fu = document.getElementById('fg-youthful_violations');
  const sync = () => { fu.hidden = !(parseInt(yd.value || '0', 10) >= 1); };
  yd.addEventListener('input', sync); sync();
  const box = document.getElementById('wc-rows');
  const renumber = () => box.querySelectorAll('fieldset.wc').forEach((fs, i) => {
    fs.querySelector('legend').textContent = 'Watercraft ' + (i + 1);
    fs.querySelector('button.remove').setAttribute('aria-label', 'Remove watercraft ' + (i + 1));
  });
  box.addEventListener('click', e => { if (e.target.matches('button.remove')) { e.target.closest('fieldset').remove(); renumber(); } });
  document.getElementById('wc-add').onclick = () => {
    box.insertAdjacentHTML('beforeend', document.getElementById('wc-template').innerHTML); renumber();
  };
})();
</script>"""


class Velloway(BasePortal):
    portal_id = "velloway"
    cookie = "vw_session"

    def fresh_draft(self) -> dict[str, Any]:
        d: dict[str, Any] = {name: "" for name in RECORD_FIELDS}
        d["watercraft"] = []
        d["umbrella_limit"] = "1000000"
        d["_account"] = ""
        return d

    def route(self, req: Request) -> Response:
        p = req.path
        if p == "/":
            return Response.redirect("/umbrella/start")
        if p == "/account/sign-in":
            return self.sign_in(req)
        if not self.has_session(req):
            if p.startswith("/umbrella/api/"):
                return Response.json_({"error": "session"}, 401)
            return Response.redirect("/account/sign-in?next=" + quote(p))
        if p == "/umbrella/start":
            return self.start(req)
        if p == "/umbrella/apply":
            return self.apply_post(req) if req.method == "POST" else self.apply_get([])
        if p == "/umbrella/api/occupations":
            slow(0.3)
            q = " ".join(req.query.get("q", "").split()).casefold()
            return Response.json_([o for o in OCCUPATIONS if q and q in o.casefold()][:8])
        if p == "/umbrella/summary":
            return self.summary([])
        if p == "/umbrella/request" and req.method == "POST":
            return self.request_quote(req)
        if p.startswith("/umbrella/quote/"):
            return self.confirmation(p.rsplit("/", 1)[1])
        return Response.html(_page("Page not found", "<h1>Page not found</h1>"), 404)

    def sign_in(self, req: Request) -> Response:
        nxt = req.query.get("next") or req.form.get("next") or "/umbrella/start"
        if not nxt.startswith("/"):
            nxt = "/umbrella/start"
        msg = ""
        if req.method == "POST":
            if self.check_login(req.form.get("email", ""), req.form.get("password", "")):
                return Response.redirect(nxt).set_cookie(self.cookie, self.new_session())
            msg = ("<div class='esum' role='alert'><h2>There is a problem</h2>"
                   "<p>The email address or password is not right.</p></div>")
        elif req.query.get("timeout"):
            msg = ("<div class='banner' role='status'><b>You were signed out</b><p>We signed you out after a period "
                   "without activity. Sign in to carry on with your quote; we saved your answers.</p></div>")
        body = f"""<h1>Sign in</h1>{msg}<form method="post" action="/account/sign-in" novalidate>
<input type="hidden" name="next" value="{esc(nxt)}">
<div class="fg"><label for="email">Email address</label><input id="email" name="email" type="email" autocomplete="username"></div>
<div class="fg"><label for="password">Password</label><input id="password" name="password" type="password" style="font:inherit;padding:6px 8px;border:2px solid #0b0c0c;width:100%;max-width:440px"></div>
<button type="submit">Sign in</button></form>"""
        return Response.html(_page("Sign in", body))

    def start(self, req: Request) -> Response:
        err = ""
        if req.method == "POST":
            n = " ".join(req.form.get("account", "").split()).upper()
            acct = self.config.get("account")
            with self.lock:
                if not n:
                    return Response.redirect("/umbrella/apply")
                if acct and acct["number"].upper() == n:
                    for k, v in acct["fields"].items():
                        self.draft[k] = json.loads(json.dumps(v))
                    self.draft["_account"] = acct["number"]
                    return Response.redirect("/umbrella/apply")
            err = "We could not find a household account with that number. Check it, or leave it blank."
        bad = " bad" if err else ""
        emsg = f"<span class='emsg' id='account-error'>{esc(err)}</span>" if err else ""
        body = f"""<h1>Get a personal umbrella quote</h1>
<p>It takes about 10 minutes. You will need details of your home, vehicles and any boats.</p>
<form method="post" action="/umbrella/start" novalidate><div class="fg{bad}"><label for="account">Household account number (optional)</label>
<div class="hint">If you already insure your home or car with us, we can fill in some answers for you. For example, VH-123456.</div>
{emsg}<input id="account" name="account" type="text" style="max-width:240px"></div><button type="submit">Continue</button></form>"""
        return Response.html(_page("Start", body))

    # ------------------------------------------------------------------
    def _fg(self, name: str, inner: str, errors: dict[str, str], hint: str = "", hidden: bool = False) -> str:
        bad = " bad" if name in errors else ""
        emsg = f"<span class='emsg' id='{name}-error'>{esc(errors[name])}</span>" if name in errors else ""
        h = f"<div class='hint'>{hint}</div>" if hint else ""
        return f"<div class='fg{bad}' id='fg-{name}' data-name='{name}'{' hidden' if hidden else ''}>{inner[0]}{h}{emsg}{inner[1]}</div>"

    def _text(self, name: str, errors: dict[str, str], kind: str = "text", hint: str = "", cls: str = "") -> str:
        v = self.draft.get(name, "")
        return self._fg(name, (f"<label for='{name}'>{esc(LABELS[name])}</label>",
                               f"<input id='{name}' name='{name}' type='{kind}' value='{esc(v)}'{cls}>"), errors, hint)

    def _date(self, name: str, errors: dict[str, str], hint: str) -> str:
        v = self.draft.get(name, "") or ""
        y, m, d = (v.split("-") + ["", "", ""])[:3] if v else ("", "", "")
        parts = "".join(
            f"<div><label for='{name}_{p}'>{t}</label><input id='{name}_{p}' name='{name}_{p}' type='text' "
            f"inputmode='numeric' class='{'w4' if p == 'year' else 'w2'}' value='{esc(str(int(val)) if val else '')}'></div>"
            for p, t, val in (("day", "Day", d), ("month", "Month", m), ("year", "Year", y)))
        return self._fg(name, (f"<fieldset role='group' aria-describedby='{name}-hint'><legend class='q'>{esc(LABELS[name])}</legend>",
                               f"<div class='dates'>{parts}</div></fieldset>"), errors,
                        f"<span id='{name}-hint'>{hint}</span>")

    def _radios(self, name: str, opts: list[tuple[str, str]], errors: dict[str, str], hidden: bool = False) -> str:
        v = str(self.draft.get(name, ""))
        items = "".join(
            f"<label><input type='radio' name='{name}' value='{esc(ov)}'{' checked' if v == ov else ''}>{esc(ot)}</label>"
            for ov, ot in opts)
        return self._fg(name, (f"<fieldset><legend class='q'>{esc(LABELS[name])}</legend>",
                               f"<div class='radios'>{items}</div></fieldset>"), errors, hidden=hidden)

    def _select(self, name: str, opts: list[tuple[str, str]], errors: dict[str, str]) -> str:
        v = str(self.draft.get(name, ""))
        o = "<option value=''>Choose</option>" + "".join(
            f"<option value='{esc(ov)}'{' selected' if v == ov else ''}>{esc(ot)}</option>" for ov, ot in opts)
        return self._fg(name, (f"<label for='{name}'>{esc(LABELS[name])}</label>",
                               f"<select id='{name}' name='{name}'>{o}</select>"), errors)

    def _wc_row(self, i: int, item: dict[str, Any]) -> str:
        opts = "<option value=''>Choose</option>" + "".join(
            f"<option{' selected' if item.get('type') == w else ''}>{w}</option>" for w in WATERCRAFT)
        n = i + 1
        return (f"<fieldset class='wc'><legend>Watercraft {n}</legend>"
                f"<div class='fg'><label>Type<select name='wc_type'>{opts}</select></label></div>"
                f"<div class='fg'><label>Length (feet)<input type='number' name='wc_length' class='w4' "
                f"value='{esc(item.get('length_ft', ''))}'></label></div>"
                f"<button type='button' class='secondary remove' aria-label='Remove watercraft {n}'>Remove</button></fieldset>")

    def apply_get(self, errors: list[tuple[str, str]]) -> Response:
        e = dict(errors)
        d = self.draft
        esum = ""
        if errors:
            items = "".join(f"<li><a href='#{k}'>{esc(m)}</a></li>" for k, m in errors)
            esum = f"<div class='esum' role='alert' tabindex='-1'><h2>There is a problem</h2><ul>{items}</ul></div>"
        banner = ""
        if d.get("_account"):
            banner = (f"<div class='banner' role='status'>We filled in some answers from household account "
                      f"<b>{esc(d['_account'])}</b>. Check every answer before you continue.</div>")
        youth = (parse_int(d.get("youthful_drivers")) or 0) >= 1
        rows = "".join(self._wc_row(i, w) for i, w in enumerate(d.get("watercraft") or []))
        occ = esc(d.get("occupation", ""))
        body = f"""<h1>Your umbrella quote</h1>{esum}{banner}<form method="post" action="/umbrella/apply" novalidate>
<h2>About you</h2>
{self._text('first_name', e)}{self._text('last_name', e)}
{self._date('dob', e, 'For example, 27 3 1980')}
{self._text('email', e, 'email')}{self._text('phone', e, 'tel')}
{self._fg('occupation', (f"<label for='occupation-input' id='occupation-label'>{LABELS['occupation']}</label>",
          f"<input id='occupation-input' role='combobox' aria-autocomplete='list' aria-expanded='false' aria-controls='occupation-list' "
          f"autocomplete='off' value='{occ}'><input type='hidden' id='occupation' name='occupation' value='{occ}'>"
          "<ul id='occupation-list' role='listbox' aria-labelledby='occupation-label' hidden></ul>"), e,
          'Start typing and choose from the list.')}
<h2>Your home</h2>
{self._text('street', e)}{self._text('city', e)}{self._select('state', [(s, s) for s in STATES], e)}{self._text('zip', e, cls=" style='max-width:160px'")}
{self._select('home_liability', HOME, e)}
{self._text('rental_properties', e, 'number', 'Enter 0 if none.')}
<h2>Drivers and vehicles</h2>
{self._text('drivers', e, 'number')}{self._text('youthful_drivers', e, 'number', 'Enter 0 if none.')}
{self._radios('youthful_violations', [(y, y) for y in YN], e, hidden=not youth)}
{self._text('vehicles', e, 'number')}{self._select('auto_liability', [(a, a) for a in AUTO], e)}
<h2>Watercraft</h2><p class="hint">Add every boat or personal watercraft someone in your household owns.</p>
<div id="wc-rows">{rows}</div><template id="wc-template">{self._wc_row(0, {})}</template>
<p><button type="button" class="secondary" id="wc-add">Add a watercraft</button></p>
<h2>Other questions</h2>
{self._radios('nonprofit_board', [(y, y) for y in YN], e)}
<h2>Your cover</h2>
{self._radios('umbrella_limit', UMBRELLA, e)}
{self._date('start_date', e, 'For example, 1 12 2026')}
<button type="submit">Continue to summary</button></form>"""
        return Response.html(_page("Your umbrella quote", body, FORM_JS))

    def apply_post(self, req: Request) -> Response:
        f = req.form
        with self.lock:
            d = self.draft
            for k in ("first_name", "last_name", "email", "phone", "street", "city", "zip"):
                d[k] = " ".join(f.get(k, "").split())
            d["dob"] = _iso(f.get("dob_day", ""), f.get("dob_month", ""), f.get("dob_year", ""))
            d["start_date"] = _iso(f.get("start_date_day", ""), f.get("start_date_month", ""), f.get("start_date_year", ""))
            d["occupation"] = f.get("occupation", "") if f.get("occupation", "") in OCCUPATIONS else ""
            for k in INT_FIELDS:
                iv = parse_int(f.get(k, ""))
                d[k] = "" if iv is None or iv < 0 else iv
            d["state"] = f.get("state", "") if f.get("state", "") in STATES else ""
            d["home_liability"] = f.get("home_liability", "") if f.get("home_liability") in dict(HOME) else ""
            d["auto_liability"] = f.get("auto_liability", "") if f.get("auto_liability") in AUTO else ""
            d["umbrella_limit"] = f.get("umbrella_limit", "") if f.get("umbrella_limit") in dict(UMBRELLA) else ""
            for k in ("youthful_violations", "nonprofit_board"):
                d[k] = f.get(k, "") if f.get(k) in YN else ""
            if (parse_int(d.get("youthful_drivers")) or 0) < 1:
                d["youthful_violations"] = ""
            types, lengths = req.form_multi.get("wc_type", []), req.form_multi.get("wc_length", [])
            d["watercraft"] = [{"type": t, "length_ft": parse_int(ln) if parse_int(ln) is not None else ""}
                               for t, ln in zip(types, lengths)]
            errors: list[tuple[str, str]] = []
            for k in RECORD_FIELDS:
                if k == "watercraft":
                    if any(w["type"] not in WATERCRAFT or w["length_ft"] in ("", 0) for w in d["watercraft"]):
                        errors.append(("wc-rows", "Enter the type and length of each watercraft"))
                    continue
                if k == "youthful_violations" and (parse_int(d.get("youthful_drivers")) or 0) < 1:
                    continue
                if d.get(k, "") == "":
                    anchor = {"dob": "dob_day", "start_date": "start_date_day", "occupation": "occupation-input"}.get(k, k)
                    msg = ("Choose an occupation from the list" if k == "occupation"
                           else f"Enter or choose: {LABELS[k].rstrip('?')}")
                    errors.append((anchor, msg))
            if errors:
                return self.apply_get(errors)
            if self.fire_once("post_apply"):
                for k, v in self.config.get("reset_on_resume", {}).items():
                    d[k] = json.loads(json.dumps(v))
                self.drop_sessions()
                return Response.redirect("/account/sign-in?timeout=1&next=" + quote("/umbrella/summary"))
        return Response.redirect("/umbrella/summary")

    # ------------------------------------------------------------------
    def _show(self, rec: dict[str, Any], k: str) -> str:
        v = rec.get(k, "")
        if k == "watercraft":
            return "<br>".join(f"{esc(w['type'])}, {esc(w['length_ft'])} feet" for w in v) or "None"
        if v == "":
            return "Not asked"
        if k in ("dob", "start_date"):
            dt = date.fromisoformat(v)
            return f"{dt.day} {dt.strftime('%B')} {dt.year}"
        if k in ("home_liability", "umbrella_limit"):
            return f"${int(v):,}"
        return esc(v)

    def _rows(self, rec: dict[str, Any], change: bool) -> str:
        out = ""
        for k in RECORD_FIELDS:
            link = f"<dd><a href='/umbrella/apply#{k}'>Change<span style='position:absolute;left:-999px'> {esc(LABELS[k])}</span></a></dd>" if change else ""
            out += f"<div><dt>{esc(LABELS[k])}</dt><dd data-k='{k}'>{self._show(rec, k)}</dd>{link}</div>"
        return out

    def _premium(self, rec: dict[str, Any]) -> int:
        base = 210 + 95 * (int(rec["umbrella_limit"]) // 1000000 - 1)
        base += 40 * max(0, int(rec["vehicles"] or 0) - 2) + 70 * int(rec["youthful_drivers"] or 0)
        base += 35 * len(rec["watercraft"]) + 55 * int(rec["rental_properties"] or 0)
        return base

    def _missing(self) -> bool:
        d = self.draft
        return any(d.get(k, "") == "" for k in RECORD_FIELDS
                   if k not in ("watercraft", "youthful_violations")) or (
            (parse_int(d.get("youthful_drivers")) or 0) >= 1 and not d.get("youthful_violations"))

    def summary(self, errors: list[str]) -> Response:
        if self._missing():
            return Response.redirect("/umbrella/apply")
        esum = ""
        bad = ""
        if errors:
            esum = ("<div class='esum' role='alert'><h2>There is a problem</h2><ul><li><a href='#confirm'>"
                    "Confirm the details are correct</a></li></ul></div>")
            bad = " bad"
        body = f"""<h1>Check your answers</h1>{esum}<dl class="sl" id="summary">{self._rows(self.draft, True)}</dl>
<p>Estimated annual premium: <b>${self._premium(self.draft):,}</b></p>
<form method="post" action="/umbrella/request" novalidate><div class="fg{bad}"><div class="radios">
<label><input type="checkbox" id="confirm" name="confirm" value="yes"> I confirm the details above are correct</label></div></div>
<button type="submit">Request quote</button></form>"""
        return Response.html(_page("Check your answers", body))

    def request_quote(self, req: Request) -> Response:
        if req.form.get("confirm") != "yes":
            return self.summary(["confirm"])
        with self.lock:
            if self._missing():
                return Response.redirect("/umbrella/apply")
            self.quote_seq += 1
            rec = {k: json.loads(json.dumps(self.draft.get(k, ""))) for k in RECORD_FIELDS}
            rec["quote_ref"] = f"VQ-{30580 + self.quote_seq}"
            self.store_submission(rec)
        return Response.redirect("/umbrella/quote/" + rec["quote_ref"])

    def confirmation(self, ref: str) -> Response:
        rec = next((r for r in self.submissions if r.get("quote_ref") == ref), None)
        if rec is None:
            return Response.html(_page("Not found", "<h1>Quote not found</h1>"), 404)
        body = (f"<div class='panel'>Quote requested<b id='quote-ref'>{esc(ref)}</b>Annual premium ${self._premium(rec):,}</div>"
                f"<h2>What we have on file</h2><dl class='sl' id='stored'>{self._rows(rec, False)}</dl>")
        return Response.html(_page("Quote requested", body))

