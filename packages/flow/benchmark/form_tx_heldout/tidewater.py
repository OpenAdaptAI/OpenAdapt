"""Tidewater Bay Insurance: home + auto bundle quick quote.

Server-rendered pages whose inputs are custom elements (``<tg-field>``,
``<tg-choice>``, ``<tg-typeahead>``) with open shadow roots: each label and
control lives inside the element's shadow DOM. "Next" collects the values in
script and posts a hidden form. The address step runs a postal
standardizer on save.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from .common import BasePortal, Request, Response, esc, parse_date, parse_int, slow

STATES = ["AZ", "CA", "CO", "ID", "MA", "ME", "NC", "NH", "OR", "RI", "VT", "WA"]
OCCUPANCY = ["Owner occupied", "Secondary", "Seasonal", "Rental"]
LOSSES = [("0", "None"), ("1", "1"), ("2", "2"), ("3", "3 or more")]
YEARS = [str(y) for y in range(2026, 2004, -1)]
DRIVERS = ["1", "2", "3", "4"]
MODELS = {
    "Halden": ["Fjord", "Fjord Touring", "Skerry"],
    "Kestrel": ["Corsa Sport", "Corsa", "Corsa Hybrid", "Ridgeback", "Ridgeback XL"],
    "Marlowe": ["Avenue", "Avenue LX", "Brook"],
    "Norvik": ["Polar", "Polar Trail", "Aster"],
    "Solenne": ["Vida Cross", "Vida", "Lune"],
}

# name, label, kind, options
STEPS: dict[int, tuple[str, list[tuple[str, str, str, Any]]]] = {
    1: ("Household and address", [
        ("first_name", "First name", "text", None),
        ("last_name", "Last name", "text", None),
        ("dob", "Date of birth (MM/DD/YYYY)", "text", None),
        ("email", "Email", "text", None),
        ("street", "Street address", "text", None),
        ("unit", "Apt / unit", "text", None),
        ("city", "City", "text", None),
        ("state", "State", "select", [(s, s) for s in STATES]),
        ("zip", "ZIP", "text", None),
    ]),
    2: ("Home", [
        ("year_built", "Year built", "text", None),
        ("sqft", "Square footage", "text", None),
        ("roof_year", "Roof updated (year)", "text", None),
        ("occupancy", "Occupancy", "select", [(o, o) for o in OCCUPANCY]),
        ("short_term_rental", "Is the home rented to others for short stays (under 30 days)?", "choice",
         [("Yes", "Yes"), ("No", "No")]),
        ("losses_3yr", "Losses in the last 3 years", "select", LOSSES),
    ]),
    3: ("Auto", [
        ("vehicle_year", "Vehicle year", "select", [(y, y) for y in YEARS]),
        ("make", "Make", "typeahead", "make"),
        ("model", "Model", "typeahead", "model"),
        ("driver_count", "Drivers in household", "select", [(d, d) for d in DRIVERS]),
    ]),
}
OPTIONAL = {"unit"}
RECORD_FIELDS = [name for _n, (_t, fs) in sorted(STEPS.items()) for name, *_ in fs]
UNIT_RE = re.compile(r"\s+(apt|apartment|unit|ste|suite|#)\s*\.?\s*[\w-]*\s*$", re.I)


def standardize(street: str) -> str:
    s = " ".join(street.split())
    s = UNIT_RE.sub("", s)
    s = re.sub(r"\s*#\s*[\w-]+\s*$", "", s)
    return s.upper()


def _rental_applies(d: dict[str, Any]) -> bool:
    return d.get("occupancy") in ("Secondary", "Seasonal")


COMPONENTS = r"""
<script>
const TG_CSS = `:host{display:block;margin:0 0 14px}label,.q{display:block;font:600 13px/1.3 Tahoma,Verdana,sans-serif;color:#0d3b4c;margin-bottom:5px}
input,select{font:14px Tahoma,Verdana,sans-serif;padding:7px 9px;border:1px solid #8fb3bf;border-radius:3px;min-width:260px;background:#fff}
.err{color:#a8231a;font:12px Tahoma,sans-serif;margin-top:3px}.opts label{display:inline-block;font-weight:normal;margin-right:14px}
ul{list-style:none;margin:2px 0 0;padding:0;border:1px solid #8fb3bf;max-width:280px;background:#fff}li{padding:6px 9px;cursor:pointer;font:14px Tahoma,sans-serif}li:hover,li[aria-selected=true]{background:#dcebf0}`;
let tgSeq = 0;
class TgBase extends HTMLElement {
  get name() { return this.getAttribute('name'); }
  errHtml() { const e = this.getAttribute('error'); return e ? '<div class="err" role="alert">' + e + '</div>' : ''; }
}
class TgField extends TgBase {
  connectedCallback() {
    const r = this.attachShadow({mode: 'open'}); const id = 'c' + (++tgSeq);
    const opts = this.getAttribute('options');
    let ctl;
    if (opts) {
      ctl = '<select id="' + id + '"><option value="">-- choose --</option>' + JSON.parse(opts).map(([v, t]) => '<option value="' + v + '">' + t + '</option>').join('') + '</select>';
    } else ctl = '<input id="' + id + '" type="text">';
    r.innerHTML = '<style>' + TG_CSS + '</style><label for="' + id + '">' + this.getAttribute('label') + '</label>' + ctl + this.errHtml();
    const c = r.getElementById(id); c.value = this.getAttribute('value') || '';
    c.addEventListener('change', () => this.dispatchEvent(new CustomEvent('tg-change', {bubbles: true, composed: true})));
  }
  get value() { return this.shadowRoot.querySelector('input,select').value; }
}
class TgChoice extends TgBase {
  connectedCallback() {
    const r = this.attachShadow({mode: 'open'}); const gid = 'g' + (++tgSeq);
    const cur = this.getAttribute('value') || '';
    r.innerHTML = '<style>' + TG_CSS + '</style><div class="q" id="' + gid + '">' + this.getAttribute('label') + '</div><div class="opts" role="radiogroup" aria-labelledby="' + gid + '">' +
      JSON.parse(this.getAttribute('options')).map(([v, t]) => { const id = 'r' + (++tgSeq); return '<label for="' + id + '"><input type="radio" name="' + gid + '" id="' + id + '" value="' + v + '"' + (v === cur ? ' checked' : '') + '> ' + t + '</label>'; }).join('') + '</div>' + this.errHtml();
  }
  get value() { const c = this.shadowRoot.querySelector('input:checked'); return c ? c.value : ''; }
}
class TgTypeahead extends TgBase {
  connectedCallback() {
    const r = this.attachShadow({mode: 'open'}); const id = 'c' + (++tgSeq), lb = 'l' + (++tgSeq);
    r.innerHTML = '<style>' + TG_CSS + '</style><label for="' + id + '">' + this.getAttribute('label') + '</label><input id="' + id + '" type="text" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="' + lb + '" autocomplete="off"><ul id="' + lb + '" role="listbox" hidden></ul>' + this.errHtml();
    const inp = r.getElementById(id), ul = r.getElementById(lb);
    this._v = this.getAttribute('value') || ''; inp.value = this._v;
    let t = null, seq = 0;
    inp.addEventListener('input', () => {
      this._v = ''; clearTimeout(t);
      t = setTimeout(async () => {
        const mine = ++seq; const params = new URLSearchParams({kind: this.getAttribute('kind'), q: inp.value});
        const dep = this.getAttribute('depends'); if (dep) params.set('make', document.querySelector('tg-typeahead[name=' + dep + ']').value);
        const res = await fetch('/qq/suggest?' + params.toString()); if (res.status === 401) { location.href = '/signin?expired=1'; return; }
        const items = await res.json(); if (mine !== seq) return;
        ul.innerHTML = ''; items.forEach((it, i) => { const li = document.createElement('li'); li.setAttribute('role', 'option'); li.id = lb + '-' + i; li.textContent = it;
          li.addEventListener('mousedown', ev => { ev.preventDefault(); inp.value = it; this._v = it; ul.hidden = true; inp.setAttribute('aria-expanded', 'false');
            this.dispatchEvent(new CustomEvent('tg-change', {bubbles: true, composed: true})); }); ul.append(li); });
        ul.hidden = !items.length; inp.setAttribute('aria-expanded', items.length ? 'true' : 'false');
      }, 150);
    });
    inp.addEventListener('blur', () => setTimeout(() => { ul.hidden = true; inp.setAttribute('aria-expanded', 'false'); }, 200));
  }
  get value() { return this._v; }
}
customElements.define('tg-field', TgField);
customElements.define('tg-choice', TgChoice);
customElements.define('tg-typeahead', TgTypeahead);
function tgSubmit(nav) {
  const f = document.getElementById('tg-post');
  f.innerHTML = '';
  const add = (n, v) => { const i = document.createElement('input'); i.type = 'hidden'; i.name = n; i.value = v; f.append(i); };
  document.querySelectorAll('tg-field,tg-choice,tg-typeahead').forEach(e => add(e.name, e.value));
  add('nav', nav); f.submit();
}
document.addEventListener('tg-change', ev => {
  const occ = document.querySelector('tg-field[name=occupancy]');
  const wrap = document.getElementById('wrap-short_term_rental');
  if (occ && wrap) wrap.hidden = !['Secondary', 'Seasonal'].includes(occ.value);
});
</script>"""

CSS = """body{margin:0;background:#e9f1f3;font-family:Tahoma,Verdana,sans-serif;color:#13303b}
.top{background:#0d3b4c;color:#fff;padding:12px 22px;font-size:17px}.top small{color:#a9cbd6;margin-left:10px;font-size:12px}
.wrap{max-width:700px;margin:22px auto;background:#fff;border:1px solid #b7cfd6;padding:20px 26px}
h1{font-size:19px;margin:0 0 4px;color:#0d3b4c}.crumbs{font-size:12px;color:#557;margin-bottom:16px}
.crumbs b{color:#0d3b4c}.note{background:#fdf5dc;border:1px solid #e5d18e;padding:8px 12px;font-size:13px;margin-bottom:14px}
.bad{color:#a8231a}button{font:14px Tahoma,sans-serif;padding:8px 16px;border:1px solid #0d3b4c;background:#fff;color:#0d3b4c;cursor:pointer;margin-right:8px}
button.go{background:#0d3b4c;color:#fff}table.rv{border-collapse:collapse;width:100%;font-size:14px}
table.rv th{text-align:left;font-weight:normal;color:#456;width:45%;padding:5px}table.rv td{padding:5px;font-weight:bold}
table.rv tr{border-bottom:1px solid #e1ebee}"""


def page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{esc(title)} | Tidewater Bay Insurance</title><style>{CSS}</style></head><body>"
        "<div class='top'>Tidewater Bay Insurance<small>Agency Quick Quote &middot; Home + Auto</small></div>"
        f"<div class='wrap'>{body}</div>{COMPONENTS}</body></html>"
    )


class Tidewater(BasePortal):
    portal_id = "tidewater"
    cookie = "TGQQ"

    def fresh_draft(self) -> dict[str, Any]:
        d: dict[str, Any] = {f: "" for f in RECORD_FIELDS}
        d.update(occupancy="Owner occupied", losses_3yr="0", driver_count="1", _file="", _notice="")
        return d

    def route(self, req: Request) -> Response:
        p = req.path
        if p in ("/", "/signin"):
            return self.signin(req)
        if not self.has_session(req):
            if p == "/qq/suggest":
                return Response.json_([], 401)
            return Response.redirect("/signin?next=" + quote(p))
        if p == "/qq/start":
            return Response.redirect("/qq/1")
        m = re.fullmatch(r"/qq/(\d)", p)
        if m and int(m.group(1)) in STEPS:
            n = int(m.group(1))
            return self.step_post(n, req) if req.method == "POST" else self.step_get(n, {})
        if p == "/qq/suggest":
            slow(0.25)
            q = req.query.get("q", "").strip().casefold()
            if not q:
                return Response.json_([])
            if req.query.get("kind") == "make":
                return Response.json_([mk for mk in MODELS if mk.casefold().startswith(q)])
            return Response.json_([md for md in MODELS.get(req.query.get("make", ""), []) if q in md.casefold()])
        if p == "/qq/review":
            if req.method == "POST":
                return self.submit(req)
            return self.review()
        m = re.fullmatch(r"/qq/done/(TQ-\d+)", p)
        if m:
            return self.done(m.group(1))
        return Response.html(page("Not found", "<h1>Not found</h1>"), 404)

    def signin(self, req: Request) -> Response:
        nxt = req.query.get("next") or req.form.get("next") or "/qq/start"
        if not nxt.startswith("/qq"):
            nxt = "/qq/start"
        msg = ""
        if req.method == "POST":
            if self.check_login(req.form.get("agent_id", ""), req.form.get("passphrase", "")):
                return Response.redirect(nxt).set_cookie(self.cookie, self.new_session())
            msg = "<p class='bad'>Sign-in failed. Check your agent ID and passphrase.</p>"
        elif req.query.get("expired"):
            msg = "<p class='bad'>For your security, your session timed out. Sign in to resume the quote.</p>"
        body = f"""<h1>Agency sign-in</h1>{msg}
<form method="post" action="/signin"><input type="hidden" name="next" value="{esc(nxt)}">
<p><label for="agent_id">Agent ID</label><br><input id="agent_id" name="agent_id" size="28"></p>
<p><label for="passphrase">Passphrase</label><br><input id="passphrase" name="passphrase" type="password" size="28"></p>
<p><button class="go" type="submit">Sign in</button></p></form>"""
        return Response.html(page("Sign in", body))

    # ------------------------------------------------------------------
    def _element(self, name: str, label: str, kind: str, opts: Any, err: str) -> str:
        v = self.draft.get(name, "")
        if name == "dob" and v:
            y, m, d = v.split("-")
            v = f"{m}/{d}/{y}"
        attrs = f'name="{name}" label="{esc(label)}" value="{esc(v)}"' + (f' error="{esc(err)}"' if err else "")
        if kind == "select":
            return f"<tg-field {attrs} options='{esc(json.dumps(opts))}'></tg-field>"
        if kind == "choice":
            return f"<tg-choice {attrs} options='{esc(json.dumps(opts))}'></tg-choice>"
        if kind == "typeahead":
            dep = ' depends="make"' if opts == "model" else ""
            return f'<tg-typeahead {attrs} kind="{opts}"{dep}></tg-typeahead>'
        return f"<tg-field {attrs}></tg-field>"

    def step_get(self, n: int, errors: dict[str, str]) -> Response:
        title, fields = STEPS[n]
        parts = []
        for name, label, kind, opts in fields:
            elem = self._element(name, label, kind, opts, errors.get(name, ""))
            if name == "short_term_rental":
                hidden = "" if _rental_applies(self.draft) else " hidden"
                elem = f"<div id='wrap-short_term_rental'{hidden}>{elem}</div>"
            parts.append(elem)
        crumbs = " &rsaquo; ".join(
            f"<b>{i}. {STEPS[i][0]}</b>" if i == n else f"{i}. {STEPS[i][0]}" for i in STEPS
        ) + " &rsaquo; 4. Review"
        note = ""
        if self.draft.get("_notice") and n in (2, 3):
            note = f"<div class='note' role='status'>{esc(self.draft['_notice'])}</div>"
        if errors:
            note += "<p class='bad' role='alert'>Some answers need attention.</p>"
        back = "<button type='button' onclick=\"tgSubmit('back')\">Back</button>" if n > 1 else ""
        body = (
            f"<h1>{esc(title)}</h1><div class='crumbs'>{crumbs}</div>{note}"
            f"<form id='tg-post' method='post' action='/qq/{n}'></form>{''.join(parts)}"
            f"<p>{back}<button type='button' class='go' onclick=\"tgSubmit('next')\">Next</button></p>"
        )
        return Response.html(page(title, body))

    def step_post(self, n: int, req: Request) -> Response:
        _t, fields = STEPS[n]
        back = req.form.get("nav") == "back"
        with self.lock:
            for name, _l, _k, _o in fields:
                if name not in req.form:
                    continue
                raw = " ".join(req.form[name].split())
                if name == "dob":
                    self.draft[name] = parse_date(raw)
                elif name == "street":
                    self.draft[name] = standardize(raw)
                elif name in ("unit", "city"):
                    self.draft[name] = raw.upper()
                elif name in ("year_built", "sqft", "roof_year"):
                    iv = parse_int(raw)
                    self.draft[name] = "" if iv is None else iv
                else:
                    self.draft[name] = raw
            if not _rental_applies(self.draft):
                self.draft["short_term_rental"] = ""
            if back:
                return Response.redirect(f"/qq/{n - 1}")
            errors = self._errors(n)
            if errors:
                return self.step_get(n, errors)
            if n == 1:
                self._customer_file()
            if self.fire_once(f"post_step_{n}"):
                self._restore_from_file()
                self.drop_sessions()
                nxt = "/qq/review" if n == 3 else f"/qq/{n + 1}"
                return Response.redirect("/signin?expired=1&next=" + quote(nxt))
        return Response.redirect("/qq/review" if n == 3 else f"/qq/{n + 1}")

    def _errors(self, n: int) -> dict[str, str]:
        d = self.draft
        e: dict[str, str] = {}
        for name, _l, kind, opts in STEPS[n][1]:
            if name in OPTIONAL:
                continue
            if name == "short_term_rental" and not _rental_applies(d):
                continue
            v = d.get(name, "")
            if v == "":
                e[name] = "Required."
            elif kind in ("select", "choice") and str(v) not in [o for o, _t in opts]:
                e[name] = "Choose an option."
        if n == 3:
            if d.get("make") not in MODELS:
                e["make"] = "Pick a make from the list."
            elif d.get("model") not in MODELS[d["make"]]:
                e["model"] = "Pick a model from the list."
        return e

    def _customer_file(self) -> None:
        cf = self.config.get("customer_file")
        if not cf or self.draft["_file"]:
            return
        if cf["last_name"].casefold() == str(self.draft["last_name"]).casefold() and cf["dob"] == self.draft["dob"]:
            self.draft["_file"] = cf["number"]
            self.draft.update(json.loads(json.dumps(cf["fields"])))
            self.draft["_notice"] = (
                f"Customer file {cf['number']} found. Home and auto details from the file were filled in; "
                "update anything that has changed."
            )

    def _restore_from_file(self) -> None:
        cf = self.config.get("customer_file")
        if cf and self.config.get("restore_file_on_resume"):
            for k in self.config["restore_file_on_resume"]:
                self.draft[k] = cf["fields"][k]

    # ------------------------------------------------------------------
    def _rows(self, rec: dict[str, Any]) -> str:
        out = ""
        for _n, (_t, fields) in sorted(STEPS.items()):
            for name, label, kind, opts in fields:
                if name == "short_term_rental" and not _rental_applies(rec):
                    continue
                v = rec.get(name, "")
                if name == "dob" and v:
                    y, m, dd = v.split("-")
                    v = f"{m}/{dd}/{y}"
                elif kind == "select" and v != "":
                    v = dict(opts).get(str(v), v)
                elif name == "sqft" and v != "":
                    v = f"{int(v):,}"
                out += f"<tr><th>{esc(label)}</th><td>{esc(v)}</td></tr>"
        return out

    def review(self) -> Response:
        body = (
            "<h1>Review</h1><div class='crumbs'>1 &rsaquo; 2 &rsaquo; 3 &rsaquo; <b>4. Review</b></div>"
            "<p>The address was standardized for rating. Check every answer before you submit.</p>"
            f"<table class='rv'>{self._rows(self.draft)}</table>"
            "<form method='post' action='/qq/review'><p>"
            "<button type='button' onclick=\"location.href='/qq/3'\">Back</button>"
            "<button type='submit' class='go'>Submit quote request</button></p></form>"
        )
        return Response.html(page("Review", body))

    def submit(self, req: Request) -> Response:
        with self.lock:
            for n in STEPS:
                if self._errors(n):
                    return Response.redirect(f"/qq/{n}")
            self.quote_seq += 1
            rec = {f: self.draft.get(f, "") for f in RECORD_FIELDS}
            rec["quote_no"] = f"TQ-{61900 + self.quote_seq}"
            self.store_submission(rec)
        return Response.redirect(f"/qq/done/{rec['quote_no']}")

    def done(self, qn: str) -> Response:
        rec = next((r for r in self.submissions if r.get("quote_no") == qn), None)
        if rec is None:
            return Response.html(page("Not found", "<h1>Not found</h1>"), 404)
        body = (
            f"<h1>Quote request submitted</h1><p>Quote number <b id='quote-no'>{esc(qn)}</b>. "
            "These answers were sent to rating:</p>"
            f"<table class='rv'>{self._rows(rec)}</table>"
        )
        return Response.html(page("Submitted", body))
