"""A small in-browser stand-in for OpenEMR's referral flow, for offline tests of the arms.

It is not OpenEMR. It copies only what the arms touch: the sign-in page, the tab
shell's ``navigateTab``/``activateTabByName``, the New Patient form's control
ids, the collapsed Contact section, the duplicate-check popup with
``#confirmCreate`` and "No matches were found.", the save POST to
``new_comprehensive_save.php``, and a REST patient search. Served through
Playwright routes on ``http://mock-openemr.test``; nothing listens on a port.

Drift kinds mirror the soak's: ``relabel`` renames the save button,
``interstitial`` puts a notice over the form that must be dismissed first.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

BASE = "http://mock-openemr.test"

LOGIN = """<!doctype html><html><body><h3>OpenEMR</h3>
<input id=authUser><input id=clearPass type=password>
<button id=login-button onclick="location.href='/interface/main/tabs/main.php'">Login</button>
</body></html>"""

MAIN = """<!doctype html><html><body><div id=tabs></div>
<script>
window.restoreSession = () => true;
window.navigateTab = (path, name) => {
  let f = document.querySelector(`iframe[name="${name}"]`);
  if (!f) { f = document.createElement('iframe'); f.name = name; f.style = 'width:1200px;height:740px';
            document.getElementById('tabs').appendChild(f); }
  f.src = path;
};
window.activateTabByName = () => true;
window.dlgopen = (url) => {
  let d = document.getElementById('dlg'); if (d) d.remove();
  d = document.createElement('iframe'); d.id = 'dlg'; d.src = url; d.style = 'position:fixed;top:60px;left:200px;width:600px;height:300px;background:#fff';
  document.body.appendChild(d);
};
window.srcConfirmSave = () => {
  const f = document.querySelector('iframe[name="openadapt-new-patient"]');
  f.contentWindow.doSave();
  document.getElementById('dlg').remove();
};
</script></body></html>"""

FORM = """<!doctype html><html><body>
%(interstitial)s
<table>
<tr><td>Title</td><td><select id=form_title><option></option><option>Mr.</option><option>Mrs.</option><option>Ms.</option></select></td></tr>
<tr><td>First Name</td><td><input id=form_fname required></td></tr>
<tr><td>Last Name</td><td><input id=form_lname required></td></tr>
<tr><td>DOB</td><td><input id=form_DOB required></td></tr>
<tr><td>Sex</td><td><select id=form_sex><option></option><option>Female</option><option>Male</option></select></td></tr>
</table>
<div style="display:none"><input id=form_street><input id=form_city></div>
<a href=# id=contact-tab onclick="document.getElementById('contact').style.display='block';return false">Contact</a>
<div id=contact style="display:none"><table>
<tr><td>Street</td><td><input id=form_street></td></tr>
<tr><td>City</td><td><input id=form_city></td></tr>
<tr><td>State</td><td><select id=form_state><option></option><option value=MA>Massachusetts</option><option value=NY>New York</option></select></td></tr>
<tr><td>Postal Code</td><td><input id=form_postal_code></td></tr>
<tr><td>Home Phone</td><td><input id=form_phone_home></td></tr>
<tr><td>Email</td><td><input id=form_email></td></tr>
<tr><td>Country</td><td><select id=form_country_code><option></option><option>USA</option></select></td></tr>
</table></div>
<button id=create onclick="openCheck()">%(save_label)s</button>
<script>
const v = id => { const all = [...document.querySelectorAll('#'+id)]; const e = all.find(x => x.offsetParent !== null) || all[all.length-1]; return e.tagName === 'SELECT' ? (e.value || '') : e.value; };
function openCheck() {
  const q = new URLSearchParams({fname: v('form_fname'), lname: v('form_lname'), DOB: v('form_DOB')});
  top.dlgopen('/interface/new/dup_check.php?' + q.toString());
}
window.doSave = () => {
  const body = {};
  for (const id of ['form_title','form_fname','form_lname','form_DOB','form_sex','form_street','form_city','form_state','form_postal_code','form_phone_home','form_email','form_country_code'])
    body[id.slice(5)] = v(id);
  fetch('/interface/new/new_comprehensive_save.php', {method: 'POST', body: JSON.stringify(body)})
    .then(r => { document.body.innerHTML = r.ok ? '<h3>Patient dashboard</h3>' : '<p class=alert>Save failed</p>'; })
    .catch(() => { document.body.innerHTML = '<p class=alert>Save failed</p>'; });
};
</script></body></html>"""

INTERSTITIAL = """<div id=notice class=alert style="position:fixed;inset:0;background:#fff;z-index:9">
<p>Release notes: the registration form has moved fields.</p>
<button id=notice-ok onclick="document.getElementById('notice').remove()">Got it</button></div>"""


@dataclass
class MockOpenEMR:
    """The store and the routes. ``patients`` is the system of record."""

    patients: list[dict[str, str]] = field(default_factory=list)
    drift: set[str] = field(default_factory=set)
    fail_next_save: int = 0
    logged_in: bool = False

    def seed(self, person: dict[str, str]) -> None:
        self.add(person)

    def add(self, p: dict[str, str]) -> dict[str, str]:
        row = {k: p.get(k, "") for k in ("title", "fname", "lname", "DOB", "sex", "street", "city",
                                         "postal_code", "phone_home", "email")}
        row["state"] = p.get("state") or ("MA" if p.get("state_label") == "Massachusetts" else p.get("state_label", ""))
        row["country_code"] = p.get("country_code", "USA")
        row["pid"] = str(len(self.patients) + 1)
        row["uuid"] = f"mock-{row['pid']}"
        self.patients.append(row)
        return row

    def search(self, **q: str) -> list[dict[str, str]]:
        def ok(p: dict[str, str]) -> bool:
            return all(not v or p.get(k, "").lower() == str(v).lower() for k, v in q.items())

        return [dict(p) for p in self.patients if ok(p)]

    # -- Playwright routes ---------------------------------------------------------

    def install(self, context: Any) -> None:
        context.route(BASE + "/**", self._route)

    def _route(self, route: Any) -> None:
        req = route.request
        url = urllib.parse.urlparse(req.url)
        path = url.path
        html = None
        if path.startswith("/interface/login"):
            html = LOGIN
        elif path == "/interface/main/tabs/main.php":
            self.logged_in = True
            html = MAIN
        elif path == "/interface/new/new_comprehensive.php":
            html = FORM % {
                "save_label": "Register patient" if "relabel" in self.drift else "Create New Patient",
                "interstitial": INTERSTITIAL if "interstitial" in self.drift else "",
            }
        elif path == "/interface/new/dup_check.php":
            q = dict(urllib.parse.parse_qsl(url.query))
            matches = self.search(fname=q.get("fname", ""), lname=q.get("lname", ""))
            if matches:
                rows = "".join(f"<li>{m['fname']} {m['lname']} {m['DOB']}</li>" for m in matches)
                html = f"<html><body><p>Possible matches</p><ul>{rows}</ul><button id=confirmCreate>Confirm Create New Patient</button></body></html>"
            else:
                html = ("<html><body><p>No matches were found.</p>"
                        "<button id=confirmCreate onclick='top.srcConfirmSave()'>Confirm Create New Patient</button>"
                        "<script>window.dlgclose = () => true;</script></body></html>")
        elif path == "/interface/new/new_comprehensive_save.php":
            if self.fail_next_save > 0:
                self.fail_next_save -= 1
                route.fulfill(status=500, body="error")
                return
            self.add(json.loads(req.post_data or "{}"))
            route.fulfill(status=200, body="ok")
            return
        if html is None:
            route.fulfill(status=404, body="not found")
        else:
            route.fulfill(status=200, content_type="text/html", body=html)


class _Resp:
    def __init__(self, data: Any) -> None:
        self.status_code = 200
        self._data = data

    def json(self) -> Any:
        return self._data


class MockRestSession:
    def __init__(self, app: MockOpenEMR) -> None:
        self.app = app

    def get(self, url: str, params: Optional[dict[str, str]] = None) -> _Resp:
        return _Resp({"data": self.app.search(**(params or {}))})


def mock_env(app: MockOpenEMR, workdir: Path) -> Any:
    """An ``Env`` for the arms, pointed at the mock."""

    from ..arm import Env

    return Env(
        app="openemr",
        ui_url=BASE,
        api_url=BASE + "/rest",
        accounts=(("clerk", "synthetic"),),
        workdir=workdir,
        seed=0,
        extra={"context_hook": app.install, "rest_session": lambda: MockRestSession(app)},
    )
