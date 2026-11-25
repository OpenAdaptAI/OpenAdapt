"""Synthetic apps for the GUI mechanism tests, served through Playwright routes.

Each app is a small in-memory server on a ``.test`` host: requests never
leave the browser process. The apps are of different kinds on purpose (a web
form, an EMR-like chart, a SaaS settings page), and none of them copies a
benchmark portal. Every record is synthetic.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import threading
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
    _origin: str = ""

    @property
    def origin(self) -> str:
        return self._origin or f"http://{self.host}"

    def dispatch(self, method: str, target: str, body: str = "") -> tuple[int, str]:
        parts = urlsplit(target)
        verb = method.upper()
        form = parse_qs(body) if body and not body.startswith("{") else {}
        if body.startswith("{"):
            form = {k: [str(v)] for k, v in json.loads(body).items()}
        form.update(parse_qs(parts.query))
        self.log.append((verb, parts.path))
        fn = self.routes.get((verb, parts.path)) or self.routes.get((verb, "*"))
        if fn is None:
            return 404, "not found"
        return fn(self, parts.path, verb, form)

    def handle(self, route: Any) -> None:
        req = route.request
        status, html = self.dispatch(req.method, req.url, req.post_data or "")
        if status in (302, 303):
            route.fulfill(status=status, headers={"Location": html}, body="")
            return
        route.fulfill(status=status, body=html, content_type="text/html")


@contextlib.contextmanager
def serve(page: Any, app: App) -> Iterator[App]:
    class _Handler(http.server.BaseHTTPRequestHandler):
        def _run(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8") if length else ""
            status, payload = app.dispatch(self.command, self.path, body)
            if status in (302, 303):
                self.send_response(status)
                self.send_header("Location", payload)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            encoded = payload.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = _run
        do_POST = _run

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    prev_origin = app._origin
    app._origin = f"http://127.0.0.1:{port}"
    try:
        yield app
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
        app._origin = prev_origin


def page(title: str, body: str) -> str:
    return PAGE.format(title=title, body=body)


# -- a web form: an event registration wizard whose next control is a link --------


def registration_app(
    *,
    next_writes: bool = False,
    commit_posts: int = 1,
    error_after: bool = False,
    fetch_writes: bool = False,
    redirect: str = "",
    saves: Optional[bool] = None,
    first_id: int = 101,
) -> App:
    """Contact details, then a ticket, then Register. "Continue" is a link that
    keeps the values in session storage. ``next_writes`` makes the link also
    post a draft (an app that changed); ``commit_posts`` is how many requests
    Register sends; ``error_after`` shows an alert after the commit.

    The extra writes are synchronous XHR, which finish before the page
    navigates; ``fetch_writes`` sends them with ``fetch()`` instead, which the
    navigation aborts. ``redirect="/done"`` saves and answers 302 to the
    confirmation page; ``redirect="/login"`` saves nothing and answers 302 to
    a plain sign-in page (an expired session), with no alert. ``saves`` says
    whether Register saves (by default, unless the redirect is ``/login``).
    ``{id}`` in ``redirect`` is the new record's id, counted from
    ``first_id`` (a post-redirect-get to the record just created)."""

    keeps = redirect != "/login" if saves is None else saves

    def _post(path: str, body: str) -> str:
        if fetch_writes:
            return f"fetch('{path}',{{method:'POST',body:{body}}});"
        return (
            "(function(){var x=new XMLHttpRequest();"
            f"x.open('POST','{path}',false);x.send({body});}})();"
        )

    draft = _post("/draft", "'{}'") if next_writes else ""

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
            _post("/register", "JSON.stringify(data())")
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
        if not keeps:
            return 302, redirect or "/login"
        records = app.state.setdefault("records", [])
        records.append({k: v[0] for k, v in form.items() if v})
        if redirect:
            return 302, redirect.replace("{id}", str(first_id + len(records) - 1))
        return done(app, path, method, form)

    def login(app: App, path: str, method: str, form: dict) -> tuple[int, str]:
        return 200, page(
            "Sign in",
            """<main><h1>Sign in</h1><p>Your session expired.</p><form>
<label for="u">Email</label><input id="u" type="email">
<button type="button">Sign in</button></form></main>""",
        )

    def done(app: App, path: str, method: str, form: dict) -> tuple[int, str]:
        alert = '<p role="alert">Payment could not be taken</p>' if error_after else ""
        return 200, page("Done", f"<main><h1>Registration received</h1>{alert}</main>")

    def landing(app: App, path: str, method: str, form: dict) -> tuple[int, str]:
        # Any other page a redirect lands on: a record, or a status page.
        status = form.get("status", [""])[0]
        return 200, page("Result", f"<main><h1>{path}</h1><p>{status}</p></main>")

    def draft_ok(app: App, path: str, method: str, form: dict) -> tuple[int, str]:
        return 200, "{}"

    return App(
        "reg.test",
        {
            ("GET", "/contact"): contact,
            ("GET", "/ticket"): ticket,
            ("POST", "/register"): register,
            ("GET", "/done"): done,
            ("GET", "/login"): login,
            ("POST", "/draft"): draft_ok,
            ("GET", "*"): landing,
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
