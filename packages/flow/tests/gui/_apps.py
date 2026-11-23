"""Synthetic apps for the GUI mechanism tests, served through Playwright routes.

Each app is a small in-memory server on a ``.test`` host: requests never
leave the browser process. The apps are of different kinds on purpose (a web
form, an EMR-like chart, a SaaS settings page), and none of them copies a
benchmark portal. Every record is synthetic.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional
from urllib.parse import parse_qs, urlsplit

Handler = Callable[["App", str, str, dict[str, list[str]]], tuple[int, str]]

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>.hidden{{display:none}}</style></head><body>{body}</body></html>"""


@dataclass
class App:
    host: str
    routes: dict[tuple[str, str], Handler]
    state: dict[str, Any] = field(default_factory=dict)
    log: list[tuple[str, str]] = field(default_factory=list)

    @property
    def origin(self) -> str:
        return f"http://{self.host}"

    def handle(self, route: Any) -> None:
        req = route.request
        parts = urlsplit(req.url)
        method = req.method.upper()
        body = req.post_data or ""
        form = parse_qs(body) if body and not body.startswith("{") else {}
        if body.startswith("{"):
            form = {k: [str(v)] for k, v in json.loads(body).items()}
        form.update(parse_qs(parts.query))
        self.log.append((method, parts.path))
        fn = self.routes.get((method, parts.path)) or self.routes.get((method, "*"))
        if fn is None:
            route.fulfill(status=404, body="not found")
            return
        status, html = fn(self, parts.path, method, form)
        if status in (302, 303):
            route.fulfill(status=status, headers={"Location": html}, body="")
            return
        route.fulfill(status=status, body=html, content_type="text/html")


@contextlib.contextmanager
def serve(page: Any, app: App) -> Iterator[App]:
    page.context.route(f"{app.origin}/**", app.handle)
    try:
        yield app
    finally:
        page.context.unroute(f"{app.origin}/**")


def page(title: str, body: str) -> str:
    return PAGE.format(title=title, body=body)


# -- a web form: an event registration wizard whose next control is a link --------


def registration_app(
    *, next_writes: bool = False, commit_posts: int = 1, error_after: bool = False
) -> App:
    """Contact details, then a ticket, then Register. "Continue" is a link that
    keeps the values in session storage. ``next_writes`` makes the link also
    post a draft (an app that changed); ``commit_posts`` is how many requests
    Register sends; ``error_after`` shows an alert after the commit."""

    draft = (
        "(function(){var x=new XMLHttpRequest();x.open('POST','/draft',false);x.send('{}');})();"
        if next_writes
        else ""
    )

    def contact(app: App, path: str, method: str, form: dict) -> tuple[int, str]:
        return 200, page(
            "Your details",
            f"""<main><h1>Your details</h1><form>
<fieldset><legend>Contact</legend>
<label for="nm">Full name</label><input id="nm">
<label for="em">Email</label><input id="em" type="email"></fieldset></form>
<nav><a href="/register" onclick="return go()">Back to events</a>
<a id="next" href="/ticket" onclick="return go()">Continue</a></nav>
<script>function go(){{sessionStorage.setItem('nm',nm.value);
sessionStorage.setItem('em',em.value);{draft}return true;}}</script></main>""",
        )

    def ticket(app: App, path: str, method: str, form: dict) -> tuple[int, str]:
        sends = "".join(
            "(function(){var x=new XMLHttpRequest();x.open('POST','/register',false);x.send(JSON.stringify(data()));})();"
            for _ in range(commit_posts - 1)
        )
        return 200, page(
            "Ticket",
            f"""<main><h1>Ticket</h1><form id="f" method="post" action="/register"
 onsubmit="fill();{sends}return true;">
<label for="tk">Ticket type</label><select id="tk" name="ticket">
<option value="">Choose</option><option value="std">Standard</option>
<option value="vip">Supporter</option></select>
<input type="hidden" name="name" id="hn"><input type="hidden" name="email" id="he">
<a href="/contact">Back</a> <button type="submit">Register</button></form>
<script>function data(){{return {{name:sessionStorage.getItem('nm'),
email:sessionStorage.getItem('em'),ticket:tk.value}};}}
function fill(){{hn.value=sessionStorage.getItem('nm');he.value=sessionStorage.getItem('em');}}
</script></main>""",
        )

    def register(app: App, path: str, method: str, form: dict) -> tuple[int, str]:
        app.state.setdefault("records", []).append(
            {k: v[0] for k, v in form.items() if v}
        )
        return done(app, path, method, form)

    def done(app: App, path: str, method: str, form: dict) -> tuple[int, str]:
        alert = '<p role="alert">Payment could not be taken</p>' if error_after else ""
        return 200, page("Done", f"<main><h1>Registration received</h1>{alert}</main>")

    def draft_ok(app: App, path: str, method: str, form: dict) -> tuple[int, str]:
        return 200, "{}"

    return App(
        "reg.test",
        {
            ("GET", "/contact"): contact,
            ("GET", "/ticket"): ticket,
            ("POST", "/register"): register,
            ("GET", "/done"): done,
            ("POST", "/draft"): draft_ok,
        },
    )


# -- an EMR-like chart whose commit is a plain link ---------------------------------


def chart_app() -> App:
    """A patient chart. "Save draft" posts in place; "Sign note" is a link
    whose GET signs the note on the server."""

    def chart(app: App, path: str, method: str, form: dict) -> tuple[int, str]:
        note = app.state.get("draft", "")
        return 200, page(
            "Chart",
            f"""<main><h1>Chart: Test Patient A</h1>
<section aria-label="Progress note"><h2>Progress note</h2>
<label for="nt">Note</label><textarea id="nt">{note}</textarea>
<button id="sd" onclick="fetch('/notes/7/draft',{{method:'POST',
body:JSON.stringify({{note:nt.value}})}})">Save draft</button>
<a href="/notes/7/sign">Sign note</a></section>
<nav aria-label="Chart navigation"><a href="/patients">Back to list</a></nav></main>""",
        )

    def save_draft(app: App, path: str, method: str, form: dict) -> tuple[int, str]:
        app.state["draft"] = form.get("note", [""])[0]
        return 200, "{}"

    def sign(app: App, path: str, method: str, form: dict) -> tuple[int, str]:
        app.state["signed"] = app.state.get("draft", "")
        return 200, page("Signed", "<main><h1>Note signed</h1></main>")

    def patients(app: App, path: str, method: str, form: dict) -> tuple[int, str]:
        return 200, page("Patients", "<main><h1>Patients</h1></main>")

    return App(
        "chart.test",
        {
            ("GET", "/patient/7"): chart,
            ("POST", "/notes/7/draft"): save_draft,
            ("GET", "/notes/7/sign"): sign,
            ("GET", "/patients"): patients,
        },
    )


# -- a SaaS settings page with tabs and a link that publishes ------------------------


def settings_app() -> App:
    def settings(app: App, path: str, method: str, form: dict) -> tuple[int, str]:
        return 200, page(
            "Settings",
            """<main><h1>Workspace settings</h1>
<div role="tablist"><button role="tab" id="t1" onclick="show(1)">Profile</button>
<button role="tab" id="t2" onclick="show(2)">Billing</button></div>
<div role="tabpanel" id="p1"><label for="ws">Workspace name</label><input id="ws"></div>
<div role="tabpanel" id="p2" class="hidden"><label for="be">Billing email</label>
<input id="be"></div>
<a href="#" id="pub" onclick="publish();return false;">Publish changes</a>
<p id="msg"></p>
<script>function show(n){p1.className=n==1?'':'hidden';p2.className=n==2?'':'hidden';}
function publish(){fetch('/api/settings',{method:'POST',body:JSON.stringify(
{ws:ws.value,be:be.value})}).then(()=>{msg.textContent='Published';});}</script></main>""",
        )

    def api(app: App, path: str, method: str, form: dict) -> tuple[int, str]:
        app.state["settings"] = {k: v[0] for k, v in form.items()}
        return 200, "{}"

    return App(
        "app.test",
        {("GET", "/settings"): settings, ("POST", "/api/settings"): api},
    )


def wait_idle(page_: Any, ms: int = 50) -> None:
    page_.wait_for_timeout(ms)


def first(seq: Any, pred: Callable[[Any], bool]) -> Optional[Any]:
    for x in seq:
        if pred(x):
            return x
    return None
