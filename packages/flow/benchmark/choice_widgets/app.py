"""A synthetic registration app built from the dropdown and popup families.

Web apps with no API often build their forms from the same few parts: a
native ``<select>``, a custom list that opens under a button, a searchable
select from a widget library, a type-ahead whose options load from the
server, a second list that reloads when the first one changes, a date
picker, a browser ``confirm()`` before the save, and a short notice after it.
This server puts each of those on its own page (``/w/<kind>``) and all of them
on one registration page (``/register``) with a follow-up dialog.

The widget libraries (jQuery, Select2, Chosen, Tom Select, flatpickr) are the
real MIT or Apache-2.0 builds in ``vendor/``. The ARIA list, the plain
``<div>`` list and the type-ahead follow the markup the common React and Vue
component libraries produce (a portal list with generated, position-based
ids), written in plain JavaScript so the fixture needs no build step.

Every saved form lands in :attr:`ChoiceApp.records`, which is the oracle: a
run counts as correct only when the record the app stored has the expected
value for every field.

Query parameters change the page at replay time, so a test can ask whether a
program finds an option by what it says or by where it was:

``order=rev``      options in reverse order
``delay=MS``       server-loaded options arrive ``MS`` milliseconds late
``missing=CODE``   the option ``CODE`` is gone
``dup=CODE``       a second option shows the same label as ``CODE``
``disabled=CODE``  the option ``CODE`` is shown but disabled
``surprise=confirm`` choosing a value raises an extra browser ``confirm()``
``surprise=modal`` a notice dialog opens over the form before the save

All data is synthetic.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

VENDOR = Path(__file__).resolve().parent / "vendor"

DEPARTMENTS: tuple[tuple[str, str], ...] = (
    ("CARD", "Cardiology"),
    ("ORTH", "Orthopaedics"),
    ("PAED", "Paediatrics"),
    ("GMED", "General Medicine"),
    ("OBG", "Obstetrics & Gynaecology"),
    ("ENT", "ENT"),
    ("DERM", "Dermatology"),
    ("NEPH", "Nephrology"),
)
DOCTORS: dict[str, tuple[tuple[str, str], ...]] = {
    "CARD": (("D11", "Dr. Asha Rao"), ("D12", "Dr. Vikram Iyer")),
    "ORTH": (("D21", "Dr. Kiran Menon"), ("D22", "Dr. Farah Khan")),
    "PAED": (("D31", "Dr. Leela Das"), ("D32", "Dr. Arjun Pillai")),
    "GMED": (("D41", "Dr. Meera Nair"), ("D42", "Dr. Rohan Shah")),
    "OBG": (("D51", "Dr. Kavya Reddy"), ("D52", "Dr. Nisha Bose")),
    "ENT": (("D61", "Dr. Omar Siddiqui"), ("D62", "Dr. Priya Joshi")),
    "DERM": (("D71", "Dr. Sunil Gupta"), ("D72", "Dr. Anita Kulkarni")),
    "NEPH": (("D81", "Dr. Ravi Varma"), ("D82", "Dr. Divya Hegde")),
}
TITLES = (("MR", "Mr"), ("MRS", "Mrs"), ("MS", "Ms"), ("DR", "Dr"), ("BABY", "Baby"))
GENDERS = (("M", "Male"), ("F", "Female"), ("O", "Other"), ("U", "Unknown"))
BLOOD = (
    ("AP", "A+"),
    ("AN", "A-"),
    ("BP", "B+"),
    ("BN", "B-"),
    ("OP", "O+"),
    ("ON", "O-"),
    ("ABP", "AB+"),
    ("ABN", "AB-"),
)
CITIES = (
    ("BLR", "Bengaluru"),
    ("BLY", "Ballari"),
    ("BEL", "Belagavi"),
    ("MYS", "Mysuru"),
    ("MNG", "Mangaluru"),
    ("HUB", "Hubballi"),
    ("CHN", "Chennai"),
    ("HYD", "Hyderabad"),
    ("PUN", "Pune"),
    ("MUM", "Mumbai"),
)
FOLLOWUP = (("1W", "1 week"), ("2W", "2 weeks"), ("1M", "1 month"), ("3M", "3 months"))

#: The widget kinds, each on its own page. The value is the field name the
#: page saves and the option catalog it shows.
WIDGETS: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "native": ("department", DEPARTMENTS),
    "aria_listbox": ("department", DEPARTMENTS),
    "div_dropdown": ("department", DEPARTMENTS),
    "select2": ("department", DEPARTMENTS),
    "chosen": ("department", DEPARTMENTS),
    "tom_select": ("department", DEPARTMENTS),
    "async_combobox": ("department", DEPARTMENTS),
    "cascade": ("department", DEPARTMENTS),
    "datepicker": ("visit_date", ()),
}


@dataclass
class Variant:
    order: str = ""
    delay_ms: int = 0
    missing: str = ""
    dup: str = ""
    disabled: str = ""
    surprise: str = ""

    @classmethod
    def from_query(cls, query: dict[str, list[str]]) -> "Variant":
        def one(key: str) -> str:
            return (query.get(key) or [""])[0]

        try:
            delay = max(0, min(5000, int(one("delay") or 0)))
        except ValueError:
            delay = 0
        return cls(
            order=one("order"),
            delay_ms=delay,
            missing=one("missing"),
            dup=one("dup"),
            disabled=one("disabled"),
            surprise=one("surprise"),
        )

    def options(self, catalog: tuple[tuple[str, str], ...]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for code, label in catalog:
            if code == self.missing:
                continue
            out.append(
                {"value": code, "label": label, "disabled": code == self.disabled}
            )
            if code == self.dup:
                out.append({"value": f"{code}-2", "label": label, "disabled": False})
        if self.order == "rev":
            out.reverse()
        return out


@dataclass
class ChoiceApp:
    records: list[dict[str, Any]] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def serve(self, port: int = 0) -> "Server":
        return Server(self, port)


class Server:
    def __init__(self, app: ChoiceApp, port: int = 0) -> None:
        self.app = app
        handler = _handler_for(app)
        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.port = int(self.http.server_address[1])
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.http.shutdown()
        self.http.server_close()


_CSS = """
body{font:14px system-ui,sans-serif;margin:24px;color:#1b1f24;background:#fff}
h1{font-size:20px;margin:0 0 16px}
form{display:grid;grid-template-columns:180px 320px;grid-auto-rows:44px;gap:8px 16px;align-items:center}
label,.lbl{font-weight:600}
input[type=text],input:not([type]),select{width:300px;padding:6px;font:inherit}
button{font:inherit;padding:6px 14px}
#save{grid-column:2;justify-self:start;margin-top:8px}
.lb-btn{width:300px;text-align:left;background:#fff;border:1px solid #888;padding:6px}
.lb-list{position:absolute;z-index:20;list-style:none;margin:0;padding:4px 0;
  width:300px;background:#fff;border:1px solid #888;box-shadow:0 4px 12px #0003}
.lb-list li{padding:6px 10px;cursor:pointer}
.lb-list li[aria-selected=true],.lb-list li:hover{background:#e6f0ff}
.lb-list li[aria-disabled=true]{color:#999}
.dd{width:288px;border:1px solid #888;padding:6px;cursor:pointer;background:#fff}
.dd-menu{position:absolute;z-index:20;background:#fff;border:1px solid #888;width:300px;
  box-shadow:0 4px 12px #0003}
.dd-item{padding:6px 10px;cursor:pointer}
.dd-item:hover{background:#eef}
.dd-item.is-disabled{color:#aaa}
.cb-list{position:absolute;z-index:20;list-style:none;margin:0;padding:4px 0;width:312px;
  background:#fff;border:1px solid #888;box-shadow:0 4px 12px #0003}
.cb-list li{padding:6px 10px;cursor:pointer}
.cb-list li.focused{background:#e6f0ff}
.cb-loading{padding:6px 10px;color:#666}
#toast{position:fixed;top:12px;right:12px;background:#1d7a35;color:#fff;padding:10px 16px;
  border-radius:6px;display:none;z-index:50}
.modal-back{position:fixed;inset:0;background:#0006;display:flex;align-items:center;
  justify-content:center;z-index:40}
.modal{background:#fff;padding:20px;border-radius:8px;min-width:360px}
.modal h2{font-size:16px;margin:0 0 12px}
.modal .row{display:flex;gap:12px;align-items:center;margin:10px 0}
#records{margin-top:24px;color:#555}
"""

_COMMON_JS = r"""
const VARIANT = __VARIANT__;
const TASK = __TASK__;
function toast(text) {
  const t = document.getElementById('toast');
  t.textContent = text; t.style.display = 'block';
  setTimeout(() => { t.style.display = 'none'; }, 2500);
}
function withQuery(path) {
  return path + (path.indexOf('?') >= 0 ? '&' : '?') + location.search.slice(1);
}
function fetchOptions(catalog, q, extra) {
  const url = withQuery('/api/options?catalog=' + encodeURIComponent(catalog)
    + '&q=' + encodeURIComponent(q || '') + (extra || ''));
  return fetch(url).then((r) => r.json());
}
function surpriseOnChoice() {
  if (VARIANT.surprise === 'confirm') {
    // An extra question the demonstration never saw.
    if (!confirm('This department has a waiting list. Continue?')) return false;
  }
  return true;
}
function maybeSurpriseModal(then) {
  if (VARIANT.surprise !== 'modal') { then(); return; }
  const back = document.createElement('div');
  back.className = 'modal-back';
  back.innerHTML = '<div class="modal" role="dialog" aria-modal="true" '
    + 'aria-labelledby="sm-t"><h2 id="sm-t">Notice</h2>'
    + '<p>The registration desk closes at 8 pm.</p>'
    + '<button type="button" id="sm-ok">OK</button></div>';
  document.body.appendChild(back);
  back.querySelector('#sm-ok').addEventListener('click', () => { back.remove(); then(); });
}
function collect() {
  const out = {task: TASK};
  document.querySelectorAll('#reg [name]').forEach((el) => {
    if (el.name) out[el.name] = el.value;
  });
  return out;
}
function save(kind, payload) {
  return fetch('/api/save', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.assign({kind: kind}, payload))}).then((r) => r.json());
}
function wireSave() {
  document.getElementById('save').addEventListener('click', () => {
    maybeSurpriseModal(() => {
      if (!confirm('Register this patient?')) return;
      save('registration', collect()).then((r) => {
        toast('Saved registration ' + r.id);
        document.getElementById('records').textContent = 'Last saved: ' + r.id;
      });
    });
  });
}

// ---- an ARIA listbox opened by a button (headless-UI style) -------------
function ariaListbox(root, name, labelText, options) {
  const id = name;
  root.innerHTML = '<span class="lbl" id="' + id + '-label">' + labelText + '</span>'
    + '<div><button type="button" class="lb-btn" id="' + id + '-btn" aria-haspopup="listbox"'
    + ' aria-expanded="false" aria-labelledby="' + id + '-label ' + id + '-btn">Select…</button>'
    + '<input type="hidden" name="' + name + '" value=""></div>';
  const btn = root.querySelector('button');
  const hidden = root.querySelector('input');
  let list = null;
  function close() { if (list) { list.remove(); list = null; } btn.setAttribute('aria-expanded', 'false'); }
  btn.addEventListener('click', () => {
    if (list) { close(); return; }
    list = document.createElement('ul');
    list.className = 'lb-list'; list.setAttribute('role', 'listbox');
    list.id = id + '-listbox';
    options.forEach((o, i) => {
      const li = document.createElement('li');
      li.setAttribute('role', 'option');
      li.id = id + '-option-' + i;   // generated from the position, as libraries do
      li.textContent = o.label;
      if (o.disabled) li.setAttribute('aria-disabled', 'true');
      li.setAttribute('aria-selected', String(hidden.value === o.value));
      li.addEventListener('click', () => {
        if (o.disabled) return;
        if (!surpriseOnChoice()) { close(); return; }
        hidden.value = o.value; btn.textContent = o.label; close();
      });
      list.appendChild(li);
    });
    const r = btn.getBoundingClientRect();
    list.style.left = (r.left + scrollX) + 'px'; list.style.top = (r.bottom + scrollY + 2) + 'px';
    document.body.appendChild(list);
    btn.setAttribute('aria-expanded', 'true');
  });
  document.addEventListener('mousedown', (e) => {
    if (list && !list.contains(e.target) && e.target !== btn) close();
  });
}

// ---- a plain <div> list with no ARIA, chosen on mousedown ---------------
function divDropdown(root, name, labelText, options) {
  root.innerHTML = '<span class="lbl">' + labelText + '</span>'
    + '<div><div class="dd" tabindex="0"><span class="dd-text">Select…</span></div>'
    + '<input type="hidden" name="' + name + '" value=""></div>';
  const trigger = root.querySelector('.dd');
  const text = root.querySelector('.dd-text');
  const hidden = root.querySelector('input');
  let menu = null;
  function close() { if (menu) { menu.remove(); menu = null; } }
  trigger.addEventListener('click', () => {
    if (menu) { close(); return; }
    menu = document.createElement('div');
    menu.className = 'dd-menu';
    options.forEach((o) => {
      const item = document.createElement('div');
      item.className = 'dd-item' + (o.disabled ? ' is-disabled' : '');
      item.textContent = o.label;
      item.addEventListener('mousedown', (e) => {
        e.preventDefault();
        if (o.disabled) return;
        if (!surpriseOnChoice()) { close(); return; }
        hidden.value = o.value; text.textContent = o.label; close();
      });
      menu.appendChild(item);
    });
    const r = trigger.getBoundingClientRect();
    menu.style.left = (r.left + scrollX) + 'px'; menu.style.top = (r.bottom + scrollY + 2) + 'px';
    document.body.appendChild(menu);
  });
}

// ---- a type-ahead whose options come from the server --------------------
function asyncCombobox(root, name, labelText, catalog) {
  const id = name;
  root.innerHTML = '<label for="' + id + '-input">' + labelText + '</label>'
    + '<div><input id="' + id + '-input" role="combobox" aria-autocomplete="list"'
    + ' aria-expanded="false" aria-controls="' + id + '-listbox" autocomplete="off"'
    + ' placeholder="Type to search">'
    + '<input type="hidden" name="' + name + '" value=""></div>';
  const input = root.querySelector('[role=combobox]');
  const hidden = root.querySelector('input[type=hidden]');
  let list = null; let seq = 0; let timer = null;
  function close() { if (list) { list.remove(); list = null; } input.setAttribute('aria-expanded', 'false'); }
  function place(el) {
    const r = input.getBoundingClientRect();
    el.style.left = (r.left + scrollX) + 'px'; el.style.top = (r.bottom + scrollY + 2) + 'px';
  }
  function render(options) {
    close();
    list = document.createElement('ul');
    list.className = 'cb-list'; list.setAttribute('role', 'listbox'); list.id = id + '-listbox';
    if (!options.length) {
      const none = document.createElement('li'); none.className = 'cb-loading';
      none.textContent = 'No options'; list.appendChild(none);
    }
    options.forEach((o, i) => {
      const li = document.createElement('li');
      li.setAttribute('role', 'option'); li.id = id + '-option-' + i;
      li.textContent = o.label;
      if (o.disabled) li.setAttribute('aria-disabled', 'true');
      li.addEventListener('click', () => {
        if (o.disabled) return;
        if (!surpriseOnChoice()) { close(); return; }
        hidden.value = o.value; input.value = o.label; close();
      });
      list.appendChild(li);
    });
    place(list); document.body.appendChild(list);
    input.setAttribute('aria-expanded', 'true');
  }
  input.addEventListener('input', () => {
    hidden.value = '';
    clearTimeout(timer);
    const q = input.value; const mine = ++seq;
    timer = setTimeout(() => {
      close();
      list = document.createElement('div'); list.className = 'cb-list cb-loading';
      list.textContent = 'Loading…'; place(list); document.body.appendChild(list);
      fetchOptions(catalog, q).then((options) => { if (mine === seq) render(options); });
    }, 150);
  });
  input.addEventListener('blur', () => setTimeout(close, 200));
}
"""


def _page(title: str, body: str, *, variant: Variant, task: str, libs: str = "") -> str:
    variant_json = json.dumps(variant.__dict__)
    js = _COMMON_JS.replace("__VARIANT__", variant_json).replace(
        "__TASK__", json.dumps(task)
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_CSS}</style>{libs}
</head><body>
<h1>{title}</h1>
<div id="toast" role="status"></div>
<script>{js}</script>
{body}
<div id="records"></div>
</body></html>"""


_JQ = '<script src="/vendor/jquery.min.js"></script>'


def _native_select(name: str, label: str, options: list[dict[str, Any]]) -> str:
    opts = "".join(
        f'<option value="{o["value"]}"{" disabled" if o["disabled"] else ""}>'
        f"{_esc(o['label'])}</option>"
        for o in options
    )
    return (
        f'<label for="{name}">{label}</label>'
        f'<select id="{name}" name="{name}"><option value="">Select…</option>{opts}</select>'
    )


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_NAME = (
    '<label for="patient_name">Patient name</label>'
    '<input id="patient_name" name="patient_name" type="text" autocomplete="off">'
)
_SAVE = '<button type="button" id="save">Save</button>'


def widget_page(kind: str, variant: Variant) -> str:
    name, catalog = WIDGETS[kind]
    options = variant.options(catalog)
    opts_json = json.dumps(options)
    libs = ""
    script = ""
    if kind == "native":
        field = _native_select(name, "Department", options)
        script = f"""document.getElementById('{name}').addEventListener('change', (e) => {{
          if (!surpriseOnChoice()) e.target.value = ''; }});"""
    elif kind == "aria_listbox":
        field = '<div id="w" style="display:contents"></div>'
        script = f"ariaListbox(document.getElementById('w'), '{name}', 'Department', {opts_json});"
    elif kind == "div_dropdown":
        field = '<div id="w" style="display:contents"></div>'
        script = f"divDropdown(document.getElementById('w'), '{name}', 'Department', {opts_json});"
    elif kind == "async_combobox":
        field = '<div id="w" style="display:contents"></div>'
        script = f"asyncCombobox(document.getElementById('w'), '{name}', 'Department', 'departments');"
    elif kind == "select2":
        libs = (
            '<link rel="stylesheet" href="/vendor/select2.min.css">'
            + _JQ
            + '<script src="/vendor/select2.min.js"></script>'
        )
        field = _native_select(name, "Department", options)
        script = f"""$('#{name}').select2({{width: '300px'}});
          $('#{name}').on('select2:selecting', (e) => {{ if (!surpriseOnChoice()) e.preventDefault(); }});"""
    elif kind == "chosen":
        libs = (
            '<link rel="stylesheet" href="/vendor/chosen.min.css">'
            + _JQ
            + '<script src="/vendor/chosen.jquery.min.js"></script>'
        )
        field = _native_select(name, "Department", options)
        script = f"""$('#{name}').chosen({{width: '300px', search_contains: true}});
          $('#{name}').on('change', () => {{ if (!surpriseOnChoice()) {{
            $('#{name}').val('').trigger('chosen:updated'); }} }});"""
    elif kind == "tom_select":
        libs = (
            '<link rel="stylesheet" href="/vendor/tom-select.css">'
            '<script src="/vendor/tom-select.complete.min.js"></script>'
        )
        field = (
            f'<label for="{name}">Department</label>'
            f'<select id="{name}" name="{name}" placeholder="Search…"></select>'
        )
        script = f"""const ts = new TomSelect('#{name}', {{
            valueField: 'value', labelField: 'label', searchField: ['label'],
            preload: false, loadThrottle: 150, closeAfterSelect: true,
            shouldLoad: (q) => q.length > 0,
            load: (q, cb) => fetchOptions('departments', q).then((opts) => cb(opts)).catch(() => cb()),
            render: {{no_results: () => '<div class="no-results">No options</div>'}},
          }});
          ts.on('item_add', () => {{ if (!surpriseOnChoice()) ts.clear(); }});"""
    elif kind == "cascade":
        field = _native_select(name, "Department", options) + (
            '<label for="doctor">Doctor</label>'
            '<select id="doctor" name="doctor" disabled><option value="">Choose a department first</option></select>'
        )
        script = f"""document.getElementById('{name}').addEventListener('change', (e) => {{
            if (!surpriseOnChoice()) {{ e.target.value = ''; return; }}
            const doc = document.getElementById('doctor');
            doc.disabled = true; doc.innerHTML = '<option value="">Loading…</option>';
            fetchOptions('doctors', '', '&dept=' + encodeURIComponent(e.target.value)).then((opts) => {{
              doc.innerHTML = '<option value="">Select…</option>' + opts.map((o) =>
                '<option value="' + o.value + '"' + (o.disabled ? ' disabled' : '') + '>'
                + o.label + '</option>').join('');
              doc.disabled = false;
            }});
          }});"""
    elif kind == "datepicker":
        libs = (
            '<link rel="stylesheet" href="/vendor/flatpickr.min.css">'
            '<script src="/vendor/flatpickr.min.js"></script>'
        )
        field = (
            f'<label for="{name}">Visit date</label>'
            f'<input id="{name}" name="{name}" type="text" placeholder="Pick a date">'
        )
        script = f"flatpickr('#{name}', {{dateFormat: 'Y-m-d', defaultDate: null}});"
    else:  # pragma: no cover - guarded by WIDGETS
        raise KeyError(kind)
    body = (
        f'<form id="reg" onsubmit="return false">{_NAME}{field}{_SAVE}</form>'
        f"<script>{script}\nwireSave();</script>"
    )
    return _page("Patient registration", body, variant=variant, task=kind, libs=libs)


def register_page(variant: Variant) -> str:
    """Every family on one page, then a follow-up job in a modal dialog."""

    libs = (
        '<link rel="stylesheet" href="/vendor/select2.min.css">'
        '<link rel="stylesheet" href="/vendor/flatpickr.min.css">'
        + _JQ
        + '<script src="/vendor/select2.min.js"></script>'
        '<script src="/vendor/flatpickr.min.js"></script>'
    )
    titles = _native_select("title", "Title", variant.options(TITLES))
    departments = _native_select(
        "department", "Department", variant.options(DEPARTMENTS)
    )
    body = f"""<form id="reg" onsubmit="return false">
{titles}
{_NAME}
<div id="w-gender" style="display:contents"></div>
<div id="w-blood" style="display:contents"></div>
{departments}
<label for="doctor">Doctor</label>
<select id="doctor" name="doctor" disabled><option value="">Choose a department first</option></select>
<div id="w-city" style="display:contents"></div>
<label for="visit_date">Visit date</label>
<input id="visit_date" name="visit_date" type="text" placeholder="Pick a date">
{_SAVE}
</form>
<p><button type="button" id="followup" disabled>Schedule follow-up</button></p>
<script>
ariaListbox(document.getElementById('w-gender'), 'gender', 'Gender', {json.dumps(variant.options(GENDERS))});
divDropdown(document.getElementById('w-blood'), 'blood_group', 'Blood group', {json.dumps(variant.options(BLOOD))});
asyncCombobox(document.getElementById('w-city'), 'city', 'City', 'cities');
$('#department').select2({{width: '300px'}});
$('#department').on('change', () => {{
  if (!surpriseOnChoice()) {{ $('#department').val('').trigger('change.select2'); return; }}
  const doc = document.getElementById('doctor');
  doc.disabled = true; doc.innerHTML = '<option value="">Loading…</option>';
  fetchOptions('doctors', '', '&dept=' + encodeURIComponent($('#department').val())).then((opts) => {{
    doc.innerHTML = '<option value="">Select…</option>' + opts.map((o) =>
      '<option value="' + o.value + '">' + o.label + '</option>').join('');
    doc.disabled = false;
  }});
}});
flatpickr('#visit_date', {{dateFormat: 'Y-m-d'}});
let lastId = null;
document.getElementById('save').addEventListener('click', () => {{
  maybeSurpriseModal(() => {{
    if (!confirm('Register this patient?')) return;
    save('registration', collect()).then((r) => {{
      lastId = r.id; toast('Saved registration ' + r.id);
      document.getElementById('records').textContent = 'Last saved: ' + r.id;
      document.getElementById('followup').disabled = false;
    }});
  }});
}});
document.getElementById('followup').addEventListener('click', () => {{
  const back = document.createElement('div');
  back.className = 'modal-back';
  back.innerHTML = '<div class="modal" role="dialog" aria-modal="true" aria-labelledby="fu-t">'
    + '<h2 id="fu-t">Follow-up</h2><div class="row"><label for="fu_in">Follow-up in</label>'
    + '<select id="fu_in" name="followup_in"><option value="">Select…</option>'
    + {json.dumps(variant.options(FOLLOWUP))}.map((o) => '<option value="' + o.value + '">'
      + o.label + '</option>').join('')
    + '</select></div><button type="button" id="fu-ok">Book follow-up</button> '
    + '<button type="button" id="fu-cancel">Cancel</button></div>';
  document.body.appendChild(back);
  back.querySelector('#fu-cancel').addEventListener('click', () => back.remove());
  back.querySelector('#fu-ok').addEventListener('click', () => {{
    save('followup', {{task: TASK, registration: lastId,
      followup_in: back.querySelector('#fu_in').value}}).then((r) => {{
      back.remove(); toast('Follow-up booked ' + r.id);
    }});
  }});
}});
</script>"""
    return _page(
        "Patient registration", body, variant=variant, task="register", libs=libs
    )


def _handler_for(app: ChoiceApp) -> type[http.server.BaseHTTPRequestHandler]:
    catalogs = {"departments": DEPARTMENTS, "cities": CITIES}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any) -> None:
            self._send(200, json.dumps(data).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            variant = Variant.from_query(query)
            with app.lock:
                app.requests.append(f"GET {parts.path}")
            if parts.path.startswith("/vendor/"):
                name = parts.path.removeprefix("/vendor/")
                path = VENDOR / name
                if "/" in name or not path.is_file():
                    self._send(404, b"not found", "text/plain")
                    return
                ctype = (
                    "text/css" if name.endswith(".css") else "application/javascript"
                )
                self._send(200, path.read_bytes(), ctype)
                return
            if parts.path.startswith("/w/"):
                kind = parts.path.removeprefix("/w/")
                if kind not in WIDGETS:
                    self._send(404, b"not found", "text/plain")
                    return
                self._send(
                    200, widget_page(kind, variant).encode(), "text/html; charset=utf-8"
                )
                return
            if parts.path == "/register":
                self._send(
                    200, register_page(variant).encode(), "text/html; charset=utf-8"
                )
                return
            if parts.path == "/api/options":
                catalog_name = (query.get("catalog") or [""])[0]
                if catalog_name == "doctors":
                    dept = (query.get("dept") or [""])[0]
                    catalog = DOCTORS.get(dept.split("-")[0], ())
                else:
                    catalog = catalogs.get(catalog_name, ())
                q = (query.get("q") or [""])[0].casefold()
                options = [
                    o
                    for o in variant.options(catalog)
                    if q in str(o["label"]).casefold()
                ]
                if variant.delay_ms:
                    time.sleep(variant.delay_ms / 1000)
                self._json(options)
                return
            if parts.path == "/api/records":
                with app.lock:
                    self._json(list(app.records))
                return
            self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            with app.lock:
                app.requests.append(f"POST {parts.path}")
            if parts.path != "/api/save":
                self._send(404, b"not found", "text/plain")
                return
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                self._send(400, b"bad json", "text/plain")
                return
            with app.lock:
                data["id"] = f"R{len(app.records) + 1:04d}"
                app.records.append(data)
            self._json({"id": data["id"]})

    return Handler


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        allow_abbrev=False, description=__doc__.splitlines()[0]
    )
    parser.add_argument("--port", type=int, default=9601)
    args = parser.parse_args(argv)
    server = ChoiceApp().serve(args.port)
    print(f"serving {server.base}/register and {server.base}/w/<kind>")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
