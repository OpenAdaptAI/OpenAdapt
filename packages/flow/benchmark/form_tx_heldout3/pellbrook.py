"""Pellbrook Marine: conversational boat and watercraft quote.

The customer signs in and answers one question per card; the server decides
the next question (some appear only for certain answers). Answered cards
stay in the thread above. A returning customer's cards come prefilled from
their last saved quote. The hull-value card has a book-value lookup. When
every question is answered the page shows a "Here's what we have" summary
with a Change button per answer, and ``Get my quote`` stores the submission
and opens the quote page.
"""

from __future__ import annotations

from typing import Any

from .case_defs import (
    PB_ENGINE_COUNT,
    PB_ENGINE_TYPE,
    PB_FIELDS,
    PB_LAYUP,
    PB_LAYUP_LABEL,
    PB_NAV,
    PB_STORAGE,
)
from .common import BasePortal, Request, Response, esc, money, parse_date, parse_int
from .estimators import book_value

RECORD_FIELDS = list(PB_FIELDS)


def _ch(values: list[Any], fmt=str) -> list[list[str]]:
    return [[str(v), fmt(v)] for v in values]


QUESTIONS: list[dict[str, Any]] = [
    {"key": "first_name", "label": "First name", "kind": "text"},
    {"key": "last_name", "label": "Last name", "kind": "text"},
    {"key": "date_of_birth", "label": "Date of birth", "kind": "date"},
    {"key": "email", "label": "Email address", "kind": "text"},
    {"key": "garaging_zip", "label": "ZIP code where the boat is kept", "kind": "text"},
    {"key": "boat_year", "label": "Model year", "kind": "int"},
    {"key": "boat_make", "label": "Manufacturer", "kind": "text"},
    {"key": "boat_model", "label": "Model", "kind": "text"},
    {"key": "length_ft", "label": "Length overall (feet)", "kind": "int"},
    {"key": "engine_type", "label": "Engine type", "kind": "choice", "options": _ch(PB_ENGINE_TYPE)},
    {"key": "total_horsepower", "label": "Total horsepower (all engines combined)", "kind": "int"},
    {"key": "engine_count", "label": "Number of engines", "kind": "choice", "options": _ch(PB_ENGINE_COUNT)},
    {"key": "agreed_hull_value", "label": "Agreed hull value", "kind": "money", "estimator": True,
     "help": "The amount we would pay for a total loss. Not sure? Look up the book value."},
    {"key": "storage", "label": "Where is the boat kept during the boating season?", "kind": "choice",
     "options": _ch(PB_STORAGE)},
    {"key": "layup_period", "label": PB_LAYUP_LABEL, "kind": "choice", "options": _ch(PB_LAYUP),
     "when": ("storage", ["Wet slip at a marina", "On a mooring"]),
     "help": "The months the boat is hauled out or laid up and not used."},
    {"key": "navigation_area", "label": "Where will you use the boat?", "kind": "choice", "options": _ch(PB_NAV)},
    {"key": "safety_course", "label": "Has the primary operator completed a boating safety course?",
     "kind": "choice", "options": [["Yes", "Yes"], ["No", "No"]]},
    {"key": "losses_3yr", "label": "Boat insurance claims in the last 3 years", "kind": "int"},
    {"key": "lienholder", "label": "Lienholder (lender with a loan on this boat)", "kind": "text",
     "optional": True, "help": "Leave blank if there is no loan."},
    {"key": "liability_limit", "label": "Liability limit", "kind": "choice",
     "options": _ch([100000, 300000, 500000], money)},
    {"key": "deductible", "label": "Hull deductible", "kind": "choice", "options": _ch([500, 1000, 2500], money)},
    {"key": "effective_date", "label": "Coverage start date", "kind": "date"},
]
QDEF = {q["key"]: q for q in QUESTIONS}
MULT = {"Single": 1, "Twin": 2, "Triple": 3}


def _display(key: str, value: Any) -> str:
    if value in ("", None):
        return "None"
    q = QDEF[key]
    if q["kind"] == "date":
        iso = parse_date(str(value))
        return f"{iso[5:7]}/{iso[8:10]}/{iso[:4]}" if iso else str(value)
    if q["kind"] == "money" or key in ("liability_limit", "deductible"):
        n = parse_int(value)
        return money(n) if n is not None else str(value)
    if q["kind"] == "int" and key != "boat_year":
        n = parse_int(value)
        return f"{n:,}" if n is not None else str(value)
    return str(value)


CSS = """
:root{--ink:#122436;--muted:#5a6a78;--sea:#14607a;--foam:#eaf4f7;--line:#cfdde4;--err:#a1281c;--bg:#f7fafb}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 Georgia,'Times New Roman',serif}
header{padding:14px 22px;border-bottom:3px solid var(--sea);background:#fff;display:flex;justify-content:space-between}
header b{font-size:20px;color:var(--sea)}main{max-width:680px;margin:22px auto;padding:0 14px}
.thread .qa{margin:0 0 10px}.thread .bq{background:#fff;border:1px solid var(--line);border-radius:14px 14px 14px 2px;padding:7px 12px;display:inline-block;color:var(--muted);font-size:14px}
.thread .ba{text-align:right}.thread .ba span{background:var(--sea);color:#fff;border-radius:14px 14px 2px 14px;padding:7px 12px;display:inline-block;font-size:14px;font-family:system-ui,Arial,sans-serif}
#card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-top:16px;box-shadow:0 2px 8px rgba(20,60,80,.06)}
#card label.q,#card legend.q{font-size:19px;display:block;margin-bottom:10px;padding:0}
#card fieldset{border:0;margin:0;padding:0}#card input[type=text]{font:16px system-ui,Arial,sans-serif;padding:9px 11px;width:100%;max-width:420px;border:1px solid #8ea6b3;border-radius:8px}
.chips label{display:inline-block;margin:0 8px 8px 0;font-family:system-ui,Arial,sans-serif;font-size:15px}
.chips input{position:absolute;opacity:0}.chips span{display:inline-block;padding:7px 14px;border:1px solid var(--sea);border-radius:999px;cursor:pointer}
.chips input:checked+span{background:var(--sea);color:#fff}.chips input:focus-visible+span{outline:2px solid #e0a100}
.note{background:var(--foam);border-radius:8px;padding:7px 11px;font-size:14px;margin:10px 0;font-family:system-ui,Arial,sans-serif}
.help{color:var(--muted);font-size:14px;margin-top:6px;font-family:system-ui,Arial,sans-serif}.err{color:var(--err);font-size:14px;font-family:system-ui,Arial,sans-serif}
button{font:15px system-ui,Arial,sans-serif;padding:9px 18px;border-radius:999px;border:1px solid var(--sea);background:var(--sea);color:#fff;cursor:pointer;margin-top:12px}
button.ghost{background:#fff;color:var(--sea)}#bv{border:1px dashed var(--sea);border-radius:10px;padding:10px 14px;margin-top:12px;font-family:system-ui,Arial,sans-serif}
#bv table td{padding:3px 14px 3px 0}table.sum{width:100%;border-collapse:collapse;font-family:system-ui,Arial,sans-serif;font-size:15px}
table.sum th,table.sum td{text-align:left;border-bottom:1px solid var(--line);padding:7px 6px;vertical-align:top}table.sum th{width:48%;font-weight:600}
table.sum button{margin:0;padding:3px 12px;font-size:13px}.banner{background:#fff7df;border:1px solid #ecd58e;border-radius:8px;padding:9px 12px;font-family:system-ui,Arial,sans-serif;font-size:14px}
"""

APP_JS = r"""
const $ = s => document.querySelector(s);
const h = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let seq = 0, busy = false;
async function api(method, url, body){
  const r = await fetch(url, {method, headers:{'Content-Type':'application/json'}, body: body ? JSON.stringify(body) : undefined});
  if (r.status === 401){ location.href = '/signin?next=/boat/quote'; throw new Error('expired'); }
  return r.json();
}
function thread(items){
  return items.map(t => `<div class="qa"><div class="bq">${h(t.label)}</div><div class="ba"><span>${h(t.display)}</span></div></div>`).join('');
}
function draw(st){
  seq += 1;
  let out = st.banner ? `<div class="banner" role="status">${h(st.banner)}</div>` : '';
  out += `<div class="thread" aria-label="Your answers so far">${thread(st.thread)}</div>`;
  if (st.summary){ out += summary(st); $('#convo').innerHTML = out; wireSummary(); $('#convo').dataset.seq = seq; $('#convo').dataset.view = 'summary'; return; }
  const q = st.card, v = q.value ?? '';
  out += `<section id="card" data-key="${q.key}" data-kind="${q.kind}">`;
  if (q.kind === 'choice'){
    out += `<fieldset><legend class="q" id="q">${h(q.label)}</legend><div class="chips" role="radiogroup" aria-labelledby="q">` +
      q.options.map((o,i) => `<label><input type="radio" name="answer" id="opt-${i}" value="${h(o[0])}"${String(v)===o[0]?' checked':''}><span>${h(o[1])}</span></label>`).join('') +
      '</div></fieldset>';
  } else {
    const ph = q.kind === 'date' ? ' placeholder="MM/DD/YYYY"' : '';
    out += `<label class="q" for="answer" id="q">${h(q.label)}</label><input type="text" id="answer" name="answer" value="${h(v)}"${ph} autocomplete="off">`;
    if (q.estimator) out += `<div><button type="button" class="ghost" id="bv-open">Look up book value</button></div><div id="bv" hidden></div>`;
  }
  if (q.prefill_note) out += `<div class="note">${h(q.prefill_note)}</div>`;
  if (q.help) out += `<div class="help">${h(q.help)}</div>`;
  out += `<div class="err" id="err" role="alert"></div><button type="button" id="next">Continue</button></section>`;
  $('#convo').innerHTML = out; $('#convo').dataset.seq = seq; $('#convo').dataset.view = 'card';
  $('#next').onclick = answer;
  const inp = $('#answer'); if (inp){ inp.addEventListener('keydown', e => { if (e.key === 'Enter'){ e.preventDefault(); answer(); } }); }
  if ($('#bv-open')) $('#bv-open').onclick = lookup;
}
async function lookup(){
  const r = await api('GET', '/api/bookvalue'); const box = $('#bv'); box.hidden = false;
  if (r.error){ box.textContent = r.error; return; }
  box.innerHTML = `<div><strong>Book value</strong> for ${h(r.boat)}</div><table>
    <tr><td>Low retail</td><td>${h(r.low_display)}</td></tr>
    <tr><td>Average retail</td><td id="bv-average">${h(r.average_display)}</td></tr>
    <tr><td>High retail</td><td>${h(r.high_display)}</td></tr></table>
    <button type="button" id="bv-use">Use average retail</button>`;
  $('#bv-use').onclick = () => { $('#answer').value = r.use_value; };
}
async function answer(){
  if (busy) return; busy = true;
  try {
    const card = $('#card'); let value = '';
    if (card.dataset.kind === 'choice'){ const c = document.querySelector('input[name=answer]:checked'); value = c ? c.value : ''; }
    else value = $('#answer').value;
    const st = await api('POST', '/api/answer', {key: card.dataset.key, value});
    if (st.error){ $('#err').textContent = st.error; $('#convo').dataset.errors = String(Date.now()); return; }
    draw(st);
  } finally { busy = false; }
}
function summary(st){
  let out = '<section id="summary-view"><h1 style="font-size:22px">Here’s what we have</h1><p class="help">Check each answer. Use Change to fix anything that is not right.</p><table class="sum" id="summary">';
  for (const r of st.summary) out += `<tr data-key="${r.key}"><th>${h(r.label)}</th><td>${h(r.display)}</td><td><button type="button" class="ghost" data-change="${r.key}" aria-label="Change ${h(r.label)}">Change</button></td></tr>`;
  return out + '</table><button type="button" id="get-quote">Get my quote</button><div class="err" id="err" role="alert"></div></section>';
}
function wireSummary(){
  document.querySelectorAll('[data-change]').forEach(b => b.onclick = async () => draw(await api('POST', '/api/edit', {key: b.dataset.change})));
  $('#get-quote').onclick = async () => {
    if (busy) return; busy = true;
    try { const r = await api('POST', '/api/submit', {}); if (r.error){ $('#err').textContent = r.error; return; } location.href = '/boat/quote/' + r.quote_ref; }
    finally { busy = false; }
  };
}
api('GET', '/api/convo').then(draw);
"""


def page(title: str, body: str) -> str:
    return (
        f"<!doctype html><html lang=en><head><meta charset=utf-8><title>{esc(title)}</title>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'><style>{CSS}</style></head><body>"
        "<header><b>Pellbrook Marine</b><span>Boat &amp; watercraft insurance</span></header>"
        f"<main>{body}</main></body></html>"
    )


class Pellbrook(BasePortal):
    portal_id = "pellbrook"
    cookie = "pbsid"

    def reset(self) -> None:
        super().reset()
        with self.lock:
            self.answers: dict[str, Any] = {}
            self.answered: list[str] = []
            self.editing: str | None = None
            self.quotes: dict[str, dict[str, Any]] = {}

    def load_case(self, case_id: str) -> bool:
        ok = super().load_case(case_id)
        if ok:
            with self.lock:
                self.answers = {}
                self.answered = []
                self.editing = None
                for k, v in (self.config.get("prior") or {}).items():
                    if k in QDEF:
                        self.answers[k] = "Yes" if v is True else "No" if v is False else str(v)
        return ok

    # ---- conversation -------------------------------------------------
    def _applies(self, q: dict[str, Any]) -> bool:
        when = q.get("when")
        return not when or self.answers.get(when[0]) in when[1]

    def _current(self) -> dict[str, Any] | None:
        if self.editing:
            return QDEF[self.editing]
        for q in QUESTIONS:
            if self._applies(q) and q["key"] not in self.answered:
                return q
        return None

    def _state(self) -> dict[str, Any]:
        prior = self.config.get("prior")
        banner = (f"Welcome back. We filled in answers from your quote of {prior['quoted_on']}. "
                  "Check each one as you go.") if prior else ""
        thread = [{"label": QDEF[k]["label"], "display": _display(k, self.answers.get(k, ""))}
                  for k in self.answered if self._applies(QDEF[k])][-6:]
        cur = self._current()
        st: dict[str, Any] = {"banner": banner, "thread": thread}
        if cur is None:
            st["thread"] = []
            st["summary"] = [{"key": q["key"], "label": q["label"], "display": _display(q["key"], self.answers.get(q["key"], ""))}
                             for q in QUESTIONS if self._applies(q)]
            return st
        card = {k: cur.get(k) for k in ("key", "label", "kind", "options", "help", "estimator")}
        card["value"] = self.answers.get(cur["key"], "")
        if prior and cur["key"] in prior and cur["key"] not in self.answered:
            card["prefill_note"] = f"From your {prior['quoted_on']} quote. Change it if it is no longer right."
        st["card"] = card
        return st

    def _clean(self, q: dict[str, Any], raw: str) -> tuple[str, str]:
        raw = (raw or "").strip()
        if not raw:
            return ("", "") if q.get("optional") else ("", "Please answer this question to continue.")
        kind = q["kind"]
        if kind == "date":
            iso = parse_date(raw)
            return (iso, "") if iso else (raw, "Enter the date as MM/DD/YYYY.")
        if kind in ("int", "money"):
            n = parse_int(raw)
            return (str(n), "") if n is not None and n >= 0 else (raw, "Enter a whole number.")
        if kind == "choice":
            return (raw, "") if raw in [o[0] for o in q["options"]] else (raw, "Choose one of the answers.")
        if q["key"] == "garaging_zip" and not (len(raw) == 5 and raw.isdigit()):
            return raw, "Enter a 5-digit ZIP code."
        return raw, ""

    def _answer(self, key: str, raw: str) -> dict[str, Any]:
        cur = self._current()
        if cur is None or cur["key"] != key:
            return {"error": "That question is no longer open. Reload the page."}
        value, err = self._clean(cur, raw)
        if err:
            return {"error": err}
        self.answers[key] = value
        if key not in self.answered:
            self.answered.append(key)
        if key == "engine_count" and MULT.get(value, 1) > 1:
            hp = parse_int(self.answers.get("total_horsepower"))
            if hp:
                self.answers["total_horsepower"] = str(hp * MULT[value])
        self.editing = None
        return self._state()

    def _record(self) -> dict[str, Any]:
        rec: dict[str, Any] = {}
        for q in QUESTIONS:
            k = q["key"]
            v = self.answers.get(k, "") if self._applies(q) else ""
            if k == "safety_course":
                rec[k] = v == "Yes"
            elif q["kind"] in ("int", "money") or k in ("liability_limit", "deductible"):
                rec[k] = parse_int(v)
            else:
                rec[k] = v
        return rec

    def _premium(self, rec: dict[str, Any]) -> int:
        p = (rec["agreed_hull_value"] or 0) * 0.0145 + {100000: 60, 300000: 95, 500000: 130}.get(
            rec["liability_limit"] or 0, 0)
        p *= {500: 1.08, 1000: 1.0, 2500: 0.9}.get(rec["deductible"] or 1000, 1.0)
        if rec["safety_course"]:
            p *= 0.95
        p += 40 * (rec["losses_3yr"] or 0)
        return int(round(p))

    # ---- routes -----------------------------------------------------
    def route(self, req: Request) -> Response:
        p = req.path
        if p in ("/", "/boat"):
            return Response.redirect("/boat/quote")
        if p == "/signin":
            return self.signin(req)
        if p.startswith("/api/"):
            if not self.has_session(req):
                return Response.json_({"error": "signed out"}, 401)
            with self.lock:
                return self.api(req)
        if not self.has_session(req):
            return Response.redirect("/signin?next=" + p)
        if p == "/boat/quote":
            body = (f"<div id=convo aria-live=polite data-seq=0><p>Loading…</p></div><script>{APP_JS}</script>")
            return Response.html(page("Get a boat quote – Pellbrook Marine", body))
        if p.startswith("/boat/quote/"):
            return self.result(p.rsplit("/", 1)[-1])
        return Response.html(page("Not found", "<h1>Not found</h1>"), 404)

    def signin(self, req: Request) -> Response:
        nxt = req.query.get("next") or req.form.get("next") or "/boat/quote"
        if not nxt.startswith("/"):
            nxt = "/boat/quote"
        msg = ""
        if req.method == "POST":
            if self.check_login(req.form.get("email", ""), req.form.get("password", "")):
                return Response.redirect(nxt).set_cookie(self.cookie, self.new_session())
            msg = "<p class=err role=alert>We couldn't sign you in with that email and password.</p>"
        body = (
            "<h1 style='font-size:24px'>Sign in to get your quote</h1>"
            f"{msg}<form method=post action='/signin'><input type=hidden name=next value='{esc(nxt)}'>"
            "<p><label for=em>Email</label><br><input type=text id=em name=email autocomplete=username "
            "style='font:16px system-ui;padding:8px;width:320px'></p>"
            "<p><label for=pw>Password</label><br><input type=password id=pw name=password "
            "style='font:16px system-ui;padding:8px;width:320px'></p><button type=submit>Sign in</button></form>"
        )
        return Response.html(page("Sign in – Pellbrook Marine", body))

    def api(self, req: Request) -> Response:
        p = req.path
        body = req.json or {}
        if p == "/api/convo":
            return Response.json_(self._state())
        if p == "/api/answer" and req.method == "POST":
            return Response.json_(self._answer(str(body.get("key", "")), str(body.get("value", ""))))
        if p == "/api/edit" and req.method == "POST":
            key = str(body.get("key", ""))
            if key in QDEF and self._current() is None:
                self.editing = key
            return Response.json_(self._state())
        if p == "/api/bookvalue":
            a = self.answers
            yr, ln, hp = parse_int(a.get("boat_year")), parse_int(a.get("length_ft")), parse_int(a.get("total_horsepower"))
            if not (yr and ln and hp and a.get("boat_make")):
                return Response.json_({"error": "Answer the boat questions first."})
            bv = book_value(yr, a["boat_make"], ln, hp)
            return Response.json_({
                "boat": f"{yr} {a['boat_make']} {a.get('boat_model', '')}".strip(),
                "low_display": money(bv["low"]), "average_display": money(bv["average"]),
                "high_display": money(bv["high"]), "use_value": f"{bv['average']:,}",
            })
        if p == "/api/submit" and req.method == "POST":
            if self._current() is not None:
                return Response.json_({"error": "Please answer every question first."})
            rec = self._record()
            self.quote_seq += 1
            rec["quote_ref"] = f"PBM-{41000 + self.quote_seq}-Q"
            rec["annual_premium"] = self._premium(rec)
            stored = self.store_submission(rec)
            self.quotes[rec["quote_ref"]] = stored
            return Response.json_({"ok": True, "quote_ref": rec["quote_ref"]})
        return Response.json_({"error": "not found"}, 404)

    def result(self, ref: str) -> Response:
        rec = self.quotes.get(ref)
        if not rec:
            return Response.html(page("Not found", "<h1>Quote not found</h1>"), 404)
        rows = "".join(
            f"<tr data-key='{q['key']}'><th>{esc(q['label'])}</th><td>"
            f"{esc(_display(q['key'], 'Yes' if rec[q['key']] is True else 'No' if rec[q['key']] is False else rec[q['key']]))}"
            "</td></tr>"
            for q in QUESTIONS
        )
        body = (
            "<h1 style='font-size:24px'>Your boat quote</h1>"
            f"<p>Quote reference <strong id=quote-ref>{esc(ref)}</strong><br>"
            f"Annual premium <strong id=premium>{esc(money(rec['annual_premium']))}</strong></p>"
            f"<table class=sum id=stored-quote>{rows}</table>"
        )
        return Response.html(page(f"Quote {ref} – Pellbrook Marine", body))
