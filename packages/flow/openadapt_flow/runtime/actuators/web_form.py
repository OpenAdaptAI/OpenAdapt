"""Repeat a recorded browser form save as one request: :class:`WebFormActuator`.

The ``web_form`` leaf of the API tier. ``compile`` proposes it from a
recording made with ``record --capture-network``
(:mod:`openadapt_flow.compiler.api_proposal`); a person admits it
(``openadapt-flow api admit``); the replayer then runs it in place of the GUI
steps it stands for.

Auth comes from the live session. Requests go through the replaying
browser's own request context, which shares its cookies, so the API step is
signed in exactly when the browser is. No credential is stored in the bundle
or read by this module.

Every run, before anything that writes is sent:

1. read the form page again, in this session, for the current CSRF or
   session value, defaults and options;
2. refuse if the form's fields or its client-side validation rules differ
   from the recording (the page changed, so the recorded request may no
   longer be what the page would send);
3. build the body: run values for recorded parameters, select values looked
   up by label in the live form, the live session value, and the form's
   defaults (each must still equal the recorded default); refuse if a field
   the page requires would be empty;
4. run the read-only pre-checks the GUI program made on screen (for example
   a duplicate search that must report no match), and refuse if a required
   text is missing.

A refusal at any of these is ``UNAVAILABLE``: the save was not sent, so the
replayer falls back to the GUI steps. After the save request starts, the
no-double-write rule of :mod:`openadapt_flow.runtime.actuators.api` applies:
any error or unaccepted response is ``HALT`` and is never retried or redone
through the GUI. The replayer then confirms the record with the same
``EffectVerifier`` as a GUI save.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse as up
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Iterable, Optional, Protocol

from openadapt_flow.runtime.actuators.api import (
    ActuationStatus,
    ApiActuationResult,
    ApiHaltKind,
)

URLENCODED = "application/x-www-form-urlencoded"

# -- HTML forms ------------------------------------------------------------------


class _FormParser(HTMLParser):
    """Named controls of each <form>, with defaults and select options."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, Any]] = []
        self._form: Optional[dict[str, Any]] = None
        self._nested = 0
        self._select: Optional[dict[str, Any]] = None
        self._option: Optional[dict[str, Any]] = None
        self._textarea: Optional[dict[str, Any]] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        a = {k: (v if v is not None else "") for k, v in attrs}
        if tag == "form":
            if self._form is not None:
                # HTML parsing ignores a <form> inside an open form; its
                # controls belong to, and are posted with, the outer form.
                self._nested += 1
                return
            self._form = {
                "action": a.get("action", ""),
                "enctype": a.get("enctype", "").lower(),
                "controls": {},
                "files": [],
                "handlers": sorted(k for k in a if k.startswith("on")),
                "handler_src": {k: v for k, v in a.items() if k.startswith("on")},
                "buttons": [],
                "novalidate": "novalidate" in a,
            }
            self.forms.append(self._form)
            return
        if self._form is None:
            return
        name = a.get("name")
        kind = a.get("type", "text" if tag == "input" else "submit").lower()
        if tag == "button" or (
            tag == "input" and kind in ("submit", "button", "image")
        ):
            # A control that can start the submit: its click handlers run
            # before the form's own.
            self._form["buttons"].append(
                {
                    "id": a.get("id"),
                    "name": name,
                    "type": kind,
                    # What the browser sends for it when it submits the form:
                    # an <input> without a value sends a localized label, so
                    # it is None (not known); a <button> without one sends "".
                    "value": a.get("value", "" if tag == "button" else None),
                    "disabled": "disabled" in a,
                    "submission_overrides": {
                        k: a[k]
                        for k in (
                            "formaction",
                            "formmethod",
                            "formenctype",
                            "formtarget",
                        )
                        if k in a
                    },
                    "handler_src": {k: v for k, v in a.items() if k.startswith("on")},
                }
            )
        if tag == "input" and name:
            if kind == "file":
                self._form["files"].append(
                    {"name": name, "multiple": "multiple" in a, "id": a.get("id")}
                )
                return
            if kind in ("submit", "button", "image", "reset"):
                return
            if kind in ("checkbox", "radio") and "checked" not in a:
                value: Optional[str] = None
            else:
                value = a.get("value", "on" if kind in ("checkbox", "radio") else "")
            self._form["controls"].setdefault(
                name,
                {"kind": kind, "default": value, "options": None, "id": a.get("id")}
                | _native_rules(a),
            )
        elif tag == "select" and name:
            self._select = {
                "id": a.get("id"),
                "kind": "select",
                "default": None,
                "options": [],
                "multiple": "multiple" in a,
            } | _native_rules(a)
            self._form["controls"].setdefault(name, self._select)
        elif tag == "option" and self._select is not None:
            self._option = {
                "value": a.get("value"),
                "label": "",
                "selected": "selected" in a,
            }
            self._select["options"].append(self._option)
        elif tag == "textarea" and name:
            self._textarea = {
                "kind": "textarea",
                "default": "",
                "options": None,
                "id": a.get("id"),
            } | _native_rules(a)
            self._form["controls"].setdefault(name, self._textarea)

    def handle_data(self, data: str) -> None:
        if self._option is not None:
            self._option["label"] += data
        elif self._textarea is not None:
            self._textarea["default"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._option is not None:
            self._option["label"] = self._option["label"].strip()
            if self._option["value"] is None:
                self._option["value"] = self._option["label"]
            self._option = None
        elif tag == "select" and self._select is not None:
            opts = self._select["options"]
            chosen = [o for o in opts if o["selected"]] or (
                [] if self._select["multiple"] else opts[:1]
            )
            self._select["default"] = chosen[0]["value"] if chosen else ""
            self._select = None
        elif tag == "textarea":
            self._textarea = None
        elif tag == "form":
            if self._nested:
                self._nested -= 1
            else:
                self._form = None


def _native_rules(a: dict[str, str]) -> dict[str, Any]:
    """A control's native HTML constraints and its script handlers."""

    return {
        "required": "required" in a,
        "pattern": a.get("pattern"),
        "min": a.get("min"),
        "max": a.get("max"),
        "minlength": a.get("minlength"),
        "maxlength": a.get("maxlength"),
        "disabled": "disabled" in a,
        "handlers": sorted(k for k in a if k.startswith("on")),
        "handler_src": {k: v for k, v in a.items() if k.startswith("on")},
    }


def _form(
    html: str,
    action_path: str,
    page_path: Optional[str] = None,
    controls: Optional[Iterable[str]] = None,
) -> Optional[dict[str, Any]]:
    parser = _FormParser()
    parser.feed(html)
    leaf = up.urlsplit(action_path).path.rsplit("/", 1)[-1]
    hits = []
    for form in parser.forms:
        action = up.urlsplit(form["action"]).path
        if not form["action"].strip() and page_path is not None:
            # No action: the browser posts to the page's own address.
            action = up.urlsplit(page_path).path
        if action == action_path or (leaf and action.rsplit("/", 1)[-1] == leaf):
            hits.append(form)
    if controls is not None and len(hits) > 1:
        # Several forms post there (a page's own settings form with no
        # action, say): the recorded one is the one with the recorded controls.
        want = sorted(controls)
        same = [f for f in hits if sorted(f["controls"]) == want]
        return same[0] if len(same) == 1 else None
    return hits[0] if hits else None


def form_controls(
    html: str, action_path: str, page_path: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """The named controls of the form in ``html`` that posts to ``action_path``.

    ``page_path`` is the address of the page itself, for a form with no
    ``action`` (it posts to that page).
    """

    form = _form(html, action_path, page_path)
    return form["controls"] if form is not None else None


def form_parts(
    html: str,
    action_path: str,
    page_path: Optional[str] = None,
    controls: Optional[Iterable[str]] = None,
) -> Optional[dict[str, Any]]:
    """The whole form that posts to ``action_path``: its named controls, its
    submit buttons (``buttons``, with the value each sends), its file inputs
    (``files``) and its ``enctype``."""

    return _form(html, action_path, page_path, controls)


def forms_of(html: str) -> list[dict[str, Any]]:
    """Every form in ``html``, parsed as :func:`form_parts` parses one."""

    parser = _FormParser()
    parser.feed(html)
    return parser.forms


def submitter_button(
    form: dict[str, Any], name: str, value: str
) -> Optional[dict[str, Any]]:
    """The one enabled submit button of ``form`` that sends ``name=value``.

    A browser sends the clicked button's own ``name=value`` with the form. The
    pair is explained only by exactly one such button whose value is written
    in the page (an ``<input type=submit>`` without a ``value`` sends a
    localized label, which the page doesn't state).
    """

    hits = [
        b
        for b in form.get("buttons") or []
        if b.get("name") == name
        and b.get("type") == "submit"
        and b.get("value") == value
        and not b.get("disabled")
    ]
    return hits[0] if len(hits) == 1 else None


_SUBMIT_LISTENER = re.compile(
    r"""addEventListener\(\s*['"]submit['"]|\.on\(\s*['"]submit['"]|\.submit\(\s*function""",
)


def form_scripts(html: str, action_path: str, sent: Iterable[str]) -> list[str]:
    """Script the browser would run on this save that the API step can't.

    The form's own ``on*`` handlers (``onsubmit``), the ``on*`` handlers of
    the fields the save sends (``onchange``, ``oninput``, ``onblur``: script
    that can validate or rewrite a value, such as capitalizing a name), and
    page script that listens for the submit. Each is named; empty when none.
    """

    form = _form(html, action_path)
    if form is None:
        return []
    found = [f"form {h}" for h in form["handlers"]]
    sent_names = set(sent)
    for name, control in form["controls"].items():
        if name in sent_names:
            found += [f"{name} {h}" for h in control.get("handlers") or []]
    if _SUBMIT_LISTENER.search(html):
        found.append("a page script listening for submit")
    return found


#: validate.js rule kinds the API step evaluates. Any other kind is script it
#: can't run (for example OpenEMR's ``pastDate``).
CLIENT_RULE_KINDS = frozenset({"presence", "length", "format", "numericality", "email"})


def unsupported_client_rules(rules: Optional[dict[str, Any]]) -> list[str]:
    """``field kind`` for every validate.js rule the API step can't evaluate."""

    out = []
    for name, rule in (rules or {}).items():
        if isinstance(rule, dict):
            out += [f"{name} {kind}" for kind in rule if kind not in CLIENT_RULE_KINDS]
    return sorted(out)


_SCRIPT_SRC = re.compile(r"""<script\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I)
_INLINE_SCRIPT = re.compile(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>\s*\S", re.I)


def _src_digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


def page_scripts(
    html: str,
    action_path: str,
    sent: Iterable[str],
    rules: Optional[dict[str, Any]],
    page_path: Optional[str] = None,
) -> list[str]:
    """Every script the browser may run on this save, fingerprinted, sorted.

    The form's own ``on*`` handlers, the ``on*`` handlers of the fields the
    save sends and of the form's buttons (each with a digest of its source),
    a page script listening for submit, every script the page loads (by
    URL), whether it has inline script, and the validate.js rule kinds
    outside :data:`CLIENT_RULE_KINDS`. Any entry means the API step runs the
    page's script before each save
    (:mod:`openadapt_flow.runtime.actuators.form_eval`), and a live page whose
    list differs doesn't run the API step.
    """

    found: list[str] = []
    form = _form(html, action_path, page_path)
    if form is not None:
        found += [f"form {h} {_src_digest(v)}" for h, v in form["handler_src"].items()]
        sent_names = set(sent)
        for name, control in form["controls"].items():
            if name in sent_names:
                found += [
                    f"{name} {h} {_src_digest(v)}"
                    for h, v in (control.get("handler_src") or {}).items()
                ]
        for button in form["buttons"]:
            label = button["id"] or button["name"] or button["type"]
            found += [
                f"button {label} {h} {_src_digest(v)}"
                for h, v in button["handler_src"].items()
            ]
    if _SUBMIT_LISTENER.search(html):
        found.append("a page script listening for submit")
    found += [f"script {src}" for src in _SCRIPT_SRC.findall(html)]
    if _INLINE_SCRIPT.search(html):
        found.append("inline script")
    found += [f"{rule} (validate.js)" for rule in unsupported_client_rules(rules)]
    return sorted(set(found))


_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _number(value: str) -> Optional[float]:
    try:
        return float(value)
    except ValueError:
        return None


def native_problem(name: str, control: dict[str, Any], value: str) -> Optional[str]:
    """Why the browser's own validation would refuse ``value``, or None.

    The constraints a browser checks before it submits: ``required``,
    ``minlength``/``maxlength``, ``pattern``, ``min``/``max`` and the input
    ``type`` (email, url, number, date). A pattern that can't be compiled is
    refused (it can't be evaluated).
    """

    if control.get("disabled") or control.get("kind") == "hidden":
        return None
    if control.get("required") and value == "":
        return f"{name} is required by the form and would be empty"
    if value == "":
        return None
    for attr, test in (
        ("minlength", lambda n: len(value) < n),
        ("maxlength", lambda n: len(value) > n),
    ):
        limit = control.get(attr)
        if limit not in (None, "") and str(limit).isdigit() and test(int(limit)):
            return f"{name} is {len(value)} characters; the form's {attr} is {limit}"
    pattern = control.get("pattern")
    if pattern:
        try:
            if re.fullmatch(f"(?:{pattern})", value) is None:
                return f"{name} doesn't match the form's pattern"
        except re.error:
            return f"{name}'s pattern can't be evaluated outside the browser"
    kind = control.get("kind")
    if kind == "email" and not all(_EMAIL.match(v.strip()) for v in value.split(",")):
        return f"{name} is not an email address"
    if kind == "url" and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
        return f"{name} is not a URL"
    if kind in ("number", "range"):
        number = _number(value)
        if number is None:
            return f"{name} is not a number"
        low = _number(str(control.get("min") or ""))
        high = _number(str(control.get("max") or ""))
        if low is not None and number < low:
            return f"{name} is outside the form's min {control.get('min')}"
        if high is not None and number > high:
            return f"{name} is outside the form's max {control.get('max')}"
    if kind == "date":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return f"{name} is not a date (YYYY-MM-DD)"
        low_date, high_date = control.get("min"), control.get("max")
        if low_date and value < str(low_date):
            return f"{name} is outside the form's min {low_date}"
        if high_date and value > str(high_date):
            return f"{name} is outside the form's max {high_date}"
    return None


def client_rule_problem(name: str, rule: dict[str, Any], value: str) -> Optional[str]:
    """Why a validate.js rule would refuse ``value``, or None."""

    if rule.get("presence") and value == "":
        return f"{name} is required by the form and would be empty"
    if value == "":
        return None
    length = rule.get("length")
    if isinstance(length, dict):
        for key, bad in (
            ("minimum", lambda n: len(value) < n),
            ("maximum", lambda n: len(value) > n),
            ("is", lambda n: len(value) != n),
        ):
            if isinstance(length.get(key), int) and bad(length[key]):
                return f"{name} has the wrong length for the form"
    fmt = rule.get("format")
    pattern = fmt.get("pattern") if isinstance(fmt, dict) else fmt
    if isinstance(pattern, str):
        try:
            if re.fullmatch(f"(?:{pattern})", value) is None:
                return f"{name} doesn't match the form's format"
        except re.error:
            return f"{name}'s format can't be evaluated outside the browser"
    if rule.get("numericality") and _number(value) is None:
        return f"{name} is not a number"
    if rule.get("email") and not _EMAIL.match(value):
        return f"{name} is not an email address"
    return None


_RULES = re.compile(r"var constraints = (\{.*?\});")


def client_rules(html: str) -> Optional[dict[str, Any]]:
    """The page's client-side validation rules, when it declares them.

    Recognizes the ``var constraints = {...}`` object that validate.js-style
    pages (OpenEMR among them) emit. The browser enforces these before it
    submits; a direct request skips them, so the API step records them,
    refuses when they change, and enforces their ``presence`` rules.
    """

    m = _RULES.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def option_for_label(control: dict[str, Any], label: str) -> Optional[str]:
    hits = [o["value"] for o in (control.get("options") or []) if o["label"] == label]
    return hits[0] if len(hits) == 1 else None


def web_form_effects(steps: Any, save: Any) -> list[Any]:
    """The record check an API step must carry: the save's, then each step write's.

    A ``step`` write (WebFormWrite) is read back by the record check declared
    on the step it was sent during, so the binding's record check is the
    save step's effects followed by those steps' effects, in write order,
    each step once.
    """

    out = list(save.effects)
    binding = getattr(save, "api_binding", None)
    spec = getattr(binding, "web_form", None)
    if spec is None:
        return out
    by_id = {s.id: s for s in steps}
    seen: set[str] = set()
    for write in spec.writes:
        if write.role != "step" or not write.from_step or write.from_step in seen:
            continue
        seen.add(write.from_step)
        step = by_id.get(write.from_step)
        if step is not None:
            out.extend(step.effects)
    return out


def folded_checks_digest(steps: Any) -> str:
    """SHA-256 over the complete definition of the steps an API step replaces.

    Every replaced step, the save included: its action and values, its
    parameters, its target and identity evidence, its guards and waits, its
    on-screen checks, its record check and its risk. Only the save's own API
    binding is left out (it carries this digest). Any change to what the GUI
    steps do or check changes the digest, and the API step doesn't run until
    it is proposed and admitted again.
    """

    import hashlib

    payload = [s.model_dump(mode="json", exclude={"api_binding"}) for s in steps]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def web_form_pointer_template(binding: Any, pointer: str) -> Optional[str]:
    """The template a ``web_form`` save sends at ``/body/<field name>``.

    ``"{param}"`` when that form field sends a run parameter as-is, else None.
    A form field's name is the page's, not the record's (``form_lname``
    versus ``lname``), so an identity binding ties the field to the record
    check by the parameter both use, not by name.
    """

    spec = getattr(binding, "web_form", None)
    if spec is None:
        return None
    root, _, name = pointer.lstrip("/").partition("/")
    if root != "body":
        return None
    for field in spec.fields:
        if field.name == name and field.source == "param" and field.param:
            return "{" + field.param + "}"
    return None


def effect_selector_rows(effect: Any) -> list[Any]:
    """The selector rows of a record check: ``match``, then ``new_records``.

    A create's record check names the new record in ``new_records``.
    """

    return [effect.match, *getattr(effect, "new_records", [])]


# -- transport -------------------------------------------------------------------


class Response(Protocol):
    status: int
    text: str
    headers: dict[str, str]


class _Resp:
    def __init__(self, status: int, text: str, headers: dict[str, str]) -> None:
        self.status, self.text, self.headers = status, text, headers


class Transport(Protocol):
    """Sends requests in the live session. ``base_url`` is the app origin."""

    base_url: str

    def get(self, path_and_query: str) -> Response: ...

    def post_form(
        self, path: str, body: str, *, referer: str, content_type: str = ...
    ) -> Response: ...

    # A save encoded as multipart calls ``post_form(..., content_type=...)``
    # with the full ``multipart/form-data; boundary=...`` type; a transport
    # that doesn't take ``content_type`` can't send one (the save fails before
    # anything leaves, and the GUI steps don't run in its place).
    # Optional: ``evaluate_form(form_path, action_path, fields) -> FormEvaluation``
    # observes native submission and the form's script on the run's values
    # (form_eval). A transport without it can't run a form that requires this
    # evidence; that save stays on the GUI.


class PlaywrightSessionTransport:
    """The browser context's own request API: same cookies, same session."""

    def __init__(self, page: Any, *, timeout_s: float = 30.0) -> None:
        parts = up.urlsplit(page.url)
        self.base_url = f"{parts.scheme}://{parts.netloc}"
        self._page = page
        self._request = page.context.request
        self._timeout_ms = timeout_s * 1000.0

    def evaluate_form(
        self,
        form_path: str,
        action_path: str,
        fields: list[tuple[str, str]],
        *,
        submitter: Optional[str] = None,
        submitter_requests: tuple[str, ...] = (),
        confirm: Optional[str] = None,
        step_writes: tuple[str, ...] = (),
        form_action: Optional[str] = None,
        encoding: str = URLENCODED,
    ) -> Any:
        from openadapt_flow.runtime.actuators.form_eval import evaluate_form

        return evaluate_form(
            self._page,
            self.base_url,
            form_path,
            action_path,
            fields,
            submitter=submitter,
            submitter_requests=submitter_requests,
            confirm=confirm,
            step_writes=step_writes,
            form_action=form_action,
            encoding=encoding,
            timeout_s=self._timeout_ms / 1000.0,
        )

    def session_header_values(
        self, names: list[str], origin: Optional[str] = None
    ) -> Optional[dict[str, str]]:
        """The values the app's own script last sent for ``names`` to
        ``origin`` (default: the page's own)."""

        from openadapt_flow.runtime.actuators import session_headers

        return session_headers.values(
            self._page.context, origin or self.base_url, names
        )

    def send_json(
        self,
        method: str,
        path: str,
        body: str,
        *,
        referer: str,
        headers: dict[str, str],
        origin: Optional[str] = None,
    ) -> Response:
        return self._wrap(
            self._request.fetch(
                (origin or self.base_url) + path,
                method=method.upper(),
                data=body.encode("utf-8"),
                headers={
                    **headers,
                    "Content-Type": "application/json",
                    "Referer": referer,
                    "Origin": self.base_url,
                },
                timeout=self._timeout_ms,
                max_redirects=0,
            )
        )

    def _wrap(self, resp: Any) -> Response:
        return _Resp(
            resp.status, resp.text(), {k.lower(): v for k, v in resp.headers.items()}
        )

    def get(self, path_and_query: str) -> Response:
        return self._wrap(
            self._request.get(
                self.base_url + path_and_query,
                timeout=self._timeout_ms,
                max_redirects=0,
            )
        )

    def post_form(
        self,
        path: str,
        body: str,
        *,
        referer: str,
        content_type: str = URLENCODED,
    ) -> Response:
        return self._wrap(
            self._request.post(
                self.base_url + path,
                data=body.encode("utf-8"),
                headers={
                    "Content-Type": content_type,
                    "Referer": referer,
                    "Origin": self.base_url,
                },
                timeout=self._timeout_ms,
                max_redirects=0,
            )
        )


# -- the prepared save ----------------------------------------------------------


def encode_multipart(
    body: Iterable[tuple[str, str]], empty_files: Iterable[str] = ()
) -> tuple[str, str]:
    """A ``multipart/form-data`` body as a browser sends a form with no file.

    Returns ``(body, content type)``. Each field is a part with its
    disposition and value; a file input left empty is a part with
    ``filename=""`` and ``application/octet-stream``, as Chromium sends it.
    The boundary is derived from the fields, so the same request always has
    the same bytes (and the same approval digest). A name or value holding a
    quote, CR or LF can't be written unambiguously and raises ``ValueError``.
    """

    pairs = list(body)
    files = set(empty_files)
    seed = json.dumps([pairs, sorted(files)], separators=(",", ":"))
    boundary = (
        "----OpenAdaptFormBoundary"
        + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    )
    out: list[str] = []
    for name, value in pairs:
        # A browser sends every line break in a value as CRLF.
        value = re.sub(r"\r\n|\r|\n", "\r\n", value)
        if any(c in name for c in '"\r\n'):
            raise ValueError(f"field name {name!r} can't be sent as multipart")
        if boundary in value:
            raise ValueError("a value contains the multipart boundary")
        out.append(f"--{boundary}\r\n")
        if name in files:
            if value:
                raise ValueError(f"{name}: the API step never attaches a file")
            out.append(
                f'Content-Disposition: form-data; name="{name}"; filename=""\r\n'
                "Content-Type: application/octet-stream\r\n\r\n\r\n"
            )
        else:
            out.append(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            )
    out.append(f"--{boundary}--\r\n")
    return "".join(out), f"multipart/form-data; boundary={boundary}"


@dataclass(frozen=True)
class PreparedSave:
    """The exact save request, built and checked, not yet sent.

    :meth:`payload_sha256` covers every byte the save sends: the method, the
    path, the content type, the encoded body (session values included) and
    the referer. A person who approves an API step approves this digest (see
    :func:`operation_digest`), and the run refuses the send when the request
    it would send has another digest.
    """

    method: str
    path: str
    body: tuple[tuple[str, str], ...]
    referer: str
    #: Fields whose value the server issued to this session (CSRF tokens).
    #: In the digest; never shown to the person.
    session_fields: frozenset[str] = frozenset()
    #: The span's ``step`` writes, sent in this order before the save.
    writes: tuple["PreparedWrite", ...] = ()
    #: The checks before saving, as reads (address, required text, label).
    #: When the span has step writes, :meth:`WebFormActuator.send` reads them
    #: again after the last write and right before the save, as the GUI
    #: checks its guard after those steps ran. Reads only: not in the digest.
    rechecks: tuple[tuple[str, tuple[str, ...], str], ...] = ()
    #: ``urlencoded`` or ``multipart`` (no file attached), as recorded.
    encoding: str = "urlencoded"
    #: Fields sent as empty file inputs in a multipart body.
    empty_files: frozenset[str] = frozenset()
    #: A ``json`` save's exact body text (``body`` then lists the run's
    #: values by JSON pointer, for the person approving it).
    json_text: str = ""
    #: Headers the app's own script sends, with this session's values.
    #: In the digest; never shown to the person.
    headers: tuple[tuple[str, str], ...] = ()
    #: The API's own origin when it isn't the page's (a ``json`` save only).
    origin: str = ""

    def encoded(self) -> str:
        if self.encoding == "json":
            return self.json_text
        if self.encoding == "multipart":
            return encode_multipart(self.body, self.empty_files)[0]
        return up.urlencode(list(self.body))

    def content_type(self) -> str:
        if self.encoding == "json":
            return "application/json"
        if self.encoding == "multipart":
            return encode_multipart(self.body, self.empty_files)[1]
        return URLENCODED

    def payload_sha256(self) -> str:
        payload: dict[str, Any] = {
            "method": self.method.upper(),
            "path": self.path,
            "content_type": self.content_type(),
            "body": self.encoded(),
            "referer": self.referer,
        }
        if self.headers:
            payload["headers"] = [list(h) for h in self.headers]
        if self.origin:
            payload["origin"] = self.origin
        if self.writes:
            # Every request the API step sends, in order. A save without step
            # writes keeps the digest it always had.
            payload["writes_before_save"] = [
                {"method": w.method.upper(), "path": w.path, "body": w.encoded()}
                for w in self.writes
            ]
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PreparedWrite:
    """One ``step`` write of the span, built and checked, not yet sent."""

    method: str
    path: str
    body: tuple[tuple[str, str], ...]
    expected_status: tuple[int, ...]
    from_step: str

    def encoded(self) -> str:
        return up.urlencode(list(self.body))


def operation_digest(
    prepared: PreparedSave,
    *,
    record_check: list[str],
    record: list[tuple[str, str]],
) -> str:
    """What a person approves for an API step, as one digest.

    Binds the exact request (:meth:`PreparedSave.payload_sha256`), the record
    the save is checked against (``record``: the record check's selector
    fields and their values for this run) and the record check itself
    (``record_check``: each effect's contract hash).
    """

    payload = {
        "schema": "openadapt.api-step-operation/v1",
        "payload_sha256": prepared.payload_sha256(),
        "record": [list(row) for row in record],
        "record_check": list(record_check),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def operation_rows(
    binding: Any,
    prepared: PreparedSave,
    params: dict[str, str],
    *,
    record: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """``(label, value)`` rows a person reads to approve an API step.

    The values this run supplies (parameters and options chosen by label),
    then the record the check reads back. The form's own defaults and the
    session values are counted, not listed: they are the recorded form's,
    and the digest covers them.
    """

    spec = binding.web_form
    sent = dict(prepared.body)
    rows: list[tuple[str, str]] = []
    defaults = 0
    for field in spec.fields:
        if field.source == "param":
            rows.append((field.name, sent.get(field.name, "")))
        elif field.source == "select_label":
            label = str(params.get(field.param or "", ""))
            rows.append((field.name, label))
        elif field.source == "form_default":
            defaults += 1
    for name, value in record:
        rows.append((f"Record check: {name}", value))
    for i, write in enumerate(prepared.writes, 1):
        rows.append(
            (
                f"Write {i} before the save",
                f"{write.method.upper()} {write.path} ({len(write.body)} field(s), from {write.from_step})",
            )
        )
    rows.append(
        (
            "Request",
            f"{prepared.method.upper()} {prepared.path}; {defaults} form default(s) "
            f"and {len(prepared.session_fields)} session value(s) as recorded",
        )
    )
    return rows


def render_operation_png(title: str, rows: list[tuple[str, str]]) -> bytes:
    """A plain image of ``rows``, shown where an approval shows a screenshot.

    Desktop and the phone show the request's image. An API step fills no
    form, so the image is the request itself, not the screen.
    """

    import io

    from PIL import Image, ImageDraw

    line_h, pad, width = 18, 16, 900
    lines = [title, ""] + [f"{label}: {value}" for label, value in rows]
    height = pad * 2 + line_h * len(lines)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for i, line in enumerate(lines):
        draw.text((pad, pad + i * line_h), line[:140], fill="black")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


# -- actuator --------------------------------------------------------------------


class WebFormActuator:
    """Run an admitted ``web_form`` binding through a live-session transport."""

    substrate = "web_form"

    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        #: One line per stage, for the run report and diagnostics. Never holds
        #: body values.
        self.log: list[dict[str, Any]] = []

    def actuate(self, binding: Any, params: dict[str, str]) -> ApiActuationResult:
        """Prepare the save and send it: :meth:`prepare`, then :meth:`send`."""

        prepared = self.prepare(binding, params)
        if isinstance(prepared, ApiActuationResult):
            return prepared
        return self.send(binding, prepared)

    def prepare(
        self, binding: Any, params: dict[str, str]
    ) -> "PreparedSave | ApiActuationResult":
        """Everything before the save: reads only, nothing that writes.

        Returns the exact request :meth:`send` would send, or an
        ``UNAVAILABLE`` result saying why the save can't be sent.
        """

        spec = binding.web_form
        summary = f"{binding.method} {binding.url_template}"

        def unavailable(reason: str) -> ApiActuationResult:
            self.log.append({"stage": "refused", "reason": reason})
            return ApiActuationResult(
                status=ActuationStatus.UNAVAILABLE,
                substrate=self.substrate,
                reason=f"{reason}; the save was not sent",
                request_summary=summary,
            )

        if spec is None:
            return unavailable("the binding has no recorded form")
        if spec.encoding == "json":
            return self._prepare_json(binding, params, unavailable)
        try:
            # Record identifiers in the URLs are run parameters (fill_url).
            form_path = fill_url(spec.form_path, params)
            save_path = fill_url(binding.url_template, params)
            page = self.transport.get(form_path)
            self.log.append({"stage": "read_form", "status": page.status})
            if page.status != 200:
                return unavailable(
                    f"the form page returned HTTP {page.status} (signed out?)"
                )
            action_path = up.urlsplit(save_path).path
            page_path = up.urlsplit(form_path).path
            form = form_parts(
                page.text,
                spec.form_action or action_path,
                page_path,
                spec.controls or None,
            )
            if form is None:
                return unavailable("the form page no longer has the recorded form")
            controls = form["controls"]
            if not spec.submitter and any(
                b.get("submission_overrides") for b in form.get("buttons") or []
            ):
                return unavailable(
                    "the recording does not identify the submitter whose overrides apply"
                )
            problem = _encoding_problem(spec, form)
            if problem:
                return unavailable(problem)
            if spec.controls and sorted(controls) != sorted(spec.controls):
                added = sorted(set(controls) - set(spec.controls))[:5]
                removed = sorted(set(spec.controls) - set(controls))[:5]
                return unavailable(
                    f"the form changed since the recording (added {added}, removed {removed})"
                )
            if client_rules(page.text) != spec.client_rules:
                return unavailable(
                    "the form's validation rules changed since the recording"
                )
            scripts = page_scripts(
                page.text,
                spec.form_action or action_path,
                [f.name for f in spec.fields],
                spec.client_rules,
                page_path,
            )
            if scripts != sorted(set(spec.client_scripts)):
                if not spec.client_scripts:
                    return unavailable(
                        "the form runs script the API step wasn't proposed to run "
                        f"({', '.join(scripts[:5])})"
                    )
                return unavailable("the form's script changed since the recording")
            if spec.form_action and not scripts:
                return unavailable(
                    "the save is sent by the page's script, and the page no longer "
                    "has script to check it against"
                )
            body, problem = self.build_body(spec, params, controls, form=form)
            if problem:
                return unavailable(problem)
            writes: list[PreparedWrite] = []
            for write in spec.writes:
                if write.role != "step":
                    continue  # the page's own request: not the API step's to send
                sub = spec.model_copy(update={"fields": write.fields})
                wbody, problem = self.build_body(
                    sub, params, controls, enforce_rules=False
                )
                if problem:
                    return unavailable(f"{write.method} {write.path}: {problem}")
                writes.append(
                    PreparedWrite(
                        method=write.method,
                        path=fill_url(write.path, params),
                        body=tuple(wbody),
                        expected_status=tuple(write.expected_status),
                        from_step=write.from_step or "",
                    )
                )
            if writes and not scripts:
                return unavailable(
                    "the API step would send writes the page's script sends, but "
                    "the page has no script to check them against"
                )
            if (
                scripts
                or spec.submitter
                or any(f.source in ("submitter", "empty_file") for f in spec.fields)
            ):
                problem = self._run_page_script(
                    spec, form_path, save_path, body, writes
                )
                if problem:
                    return unavailable(problem)
            rechecks = []
            for check in spec.prechecks:
                query = [(k, _fill(v, params)) for k, v in check.query]
                qs = "&".join(
                    up.quote(k) + ("=" + up.quote(v) if v else "") for k, v in query
                )
                read = (
                    check.path + (f"?{qs}" if qs else ""),
                    tuple(check.require_text),
                    check.from_step or check.path,
                )
                problem = self._precheck(read)
                if problem:
                    return unavailable(problem)
                rechecks.append(read)
        except KeyError as exc:
            return unavailable(f"the run has no value for {exc}")
        except Exception as exc:  # noqa: BLE001 - only reads were sent
            return unavailable(f"a read before the save failed ({type(exc).__name__})")
        return PreparedSave(
            method=binding.method,
            path=save_path,
            body=tuple(body),
            referer=self.transport.base_url + form_path,
            session_fields=frozenset(
                f.name for f in spec.fields if f.source == "session"
            ),
            writes=tuple(writes),
            rechecks=tuple(rechecks),
            encoding=spec.encoding,
            empty_files=frozenset(
                f.name for f in spec.fields if f.source == "empty_file"
            ),
        )

    def _prepare_json(
        self, binding: Any, params: dict[str, str], unavailable: Any
    ) -> "PreparedSave | ApiActuationResult":
        """A JSON save: the recorded body with the run's values, and the
        headers the app's own script sends in this session."""

        spec = binding.web_form
        try:
            form_path = fill_url(spec.form_path, params)
            save_path = fill_url(binding.url_template, params)
            if spec.writes or spec.prechecks:
                return unavailable("a JSON save is sent alone, with no other request")
            page = self.transport.get(form_path)
            self.log.append({"stage": "read_page", "status": page.status})
            if page.status != 200:
                return unavailable(
                    f"the page the save is sent from returned HTTP {page.status}"
                )
            text, values, problem = fill_json(spec, params)
            if problem:
                return unavailable(problem)
            headers: dict[str, str] = {}
            if spec.session_headers:
                read = getattr(self.transport, "session_header_values", None)
                found = None
                if read is not None:
                    found = (
                        read(list(spec.session_headers), spec.api_origin)
                        if spec.api_origin
                        else read(list(spec.session_headers))
                    )
                if found is None:
                    return unavailable(
                        "the app's script hasn't sent its "
                        f"{', '.join(spec.session_headers)} header(s) "
                        f"{'to ' + spec.api_origin + ' ' if spec.api_origin else ''}"
                        "in this session"
                    )
                headers = found
        except KeyError as exc:
            return unavailable(f"the run has no value for {exc}")
        except Exception as exc:  # noqa: BLE001 - only reads were sent
            return unavailable(f"a read before the save failed ({type(exc).__name__})")
        return PreparedSave(
            method=binding.method,
            path=save_path,
            body=tuple(values),
            referer=self.transport.base_url + form_path,
            encoding="json",
            json_text=text,
            headers=tuple(sorted(headers.items())),
            origin=spec.api_origin or "",
        )

    def _precheck(self, read: tuple[str, tuple[str, ...], str]) -> Optional[str]:
        """Read one check before saving; why it fails, or None."""

        url, require_text, label = read
        resp = self.transport.get(url)
        missing = [t for t in require_text if t not in resp.text]
        self.log.append(
            {
                "stage": "precheck",
                "path": up.urlsplit(url).path,
                "status": resp.status,
                "missing": missing,
            }
        )
        if resp.status != 200 or missing:
            return (
                f"the check before saving ({label}) did not pass: missing {missing!r}"
            )
        return None

    def _run_page_script(
        self,
        spec: Any,
        form_path: str,
        action_path: str,
        body: list[tuple[str, str]],
        writes: list["PreparedWrite"] = (),  # type: ignore[assignment]
    ) -> Optional[str]:
        """Observe the form's native submit and script; why not to send, or None.

        The fields the person typed or picked are typed into a scratch copy
        of the form, and the recorded controls are clicked (form_eval). The
        save is sent only when the page submits, once, and the body it
        submits is exactly ``body`` apart from the session values the scratch
        page was issued for itself. Any difference keeps the save on the GUI.
        """

        evaluate = getattr(self.transport, "evaluate_form", None)
        if evaluate is None:
            return "the form requires browser evaluation and this session can't run it"
        typed = {f.name for f in spec.fields if f.source in ("param", "select_label")}
        result = evaluate(
            form_path,
            action_path,
            [(k, v) for k, v in body if k in typed],
            submitter=spec.submitter,
            submitter_requests=tuple(spec.submitter_requests),
            confirm=spec.confirm,
            step_writes=tuple(f"{w.method} {up.urlsplit(w.path).path}" for w in writes),
            **({"form_action": spec.form_action} if spec.form_action else {}),
            **(
                {"encoding": "multipart/form-data"}
                if spec.encoding == "multipart"
                else {}
            ),
        )
        self.log.append(
            {
                "stage": "page_script",
                "status": result.status,
                "via": result.via,
                "seconds": result.seconds,
                "blocked": result.blocked,
            }
        )
        if not result.valid:
            return f"{result.reason}; the browser did not reproduce the recorded submit"
        session = {f.name for f in spec.fields if f.source == "session"}
        want = sorted((k, v) for k, v in body if k not in session)
        got = sorted((k, v) for k, v in result.serialized or [] if k not in session)
        if want != got:
            changed = sorted({k for k, _ in set(want) ^ set(got)})
            return (
                "the page would send other values than the API step built "
                f"({', '.join(changed[:5])}); its script changes them"
            )
        # Each write the API step sends before the save must be one the
        # page's own script sent in the scratch run, with the same body.
        sent = list(getattr(result, "writes", None) or [])
        for write in writes:
            what = f"{write.method} {up.urlsplit(write.path).path}"
            want_w = sorted((k, v) for k, v in write.body if k not in session)
            match = next(
                (
                    i
                    for i, (w, b) in enumerate(sent)
                    if w == what
                    and b is not None
                    and sorted((k, v) for k, v in b if k not in session) == want_w
                ),
                None,
            )
            if match is not None:
                sent.pop(match)  # each request the page sent matches one write
            if match is None:
                return (
                    f"the page's script didn't send {what} with the body the API step "
                    "built when it ran on these values"
                )
        if sent:
            return (
                "the page's script sent more step writes than the API step would "
                f"({', '.join(w for w, _ in sent[:3])})"
            )
        return None

    def send(self, binding: Any, prepared: "PreparedSave") -> ApiActuationResult:
        """Send exactly ``prepared``, once. Never retried."""

        spec = binding.web_form
        summary = f"{binding.method} {binding.url_template}"
        total = len(prepared.writes) + 1
        for i, write in enumerate(prepared.writes, 1):
            what = f"{write.method} {write.path}"
            done = f"; the {i - 1} write(s) before it were accepted" if i > 1 else ""
            try:
                wresp = self.transport.post_form(
                    write.path, write.encoded(), referer=prepared.referer
                )
            except Exception as exc:  # noqa: BLE001 - after dispatch began
                self.log.append({"stage": "write", "n": i, "error": type(exc).__name__})
                return ApiActuationResult(
                    status=ActuationStatus.HALT,
                    substrate=self.substrate,
                    halt_kind=ApiHaltKind.DELIVERY_UNCERTAIN,
                    reason=(
                        f"write {i} of {total} ({what}) failed after it was sent "
                        f"({type(exc).__name__}){done}; it may have written, nothing "
                        "after it was sent and nothing is retried"
                    ),
                    request_summary=summary,
                )
            accepted = wresp.status in (write.expected_status or tuple(range(200, 400)))
            self.log.append(
                {"stage": "write", "n": i, "status": wresp.status, "accepted": accepted}
            )
            if not accepted:
                return ApiActuationResult(
                    status=ActuationStatus.HALT,
                    substrate=self.substrate,
                    halt_kind=ApiHaltKind.RESPONSE_REJECTED,
                    reason=(
                        f"write {i} of {total} ({what}) -> {wresp.status}, outside its "
                        f"recorded response{done}; the save was not sent and nothing is retried"
                    ),
                    http_status=wresp.status,
                    request_summary=summary,
                )
        if prepared.writes:
            # The step writes ran; the GUI checks its guard (for example the
            # duplicate search's "No matches were found.") after them, right
            # before the save. Read the checks again here, in that order.
            sent = len(prepared.writes)
            for read in prepared.rechecks:
                try:
                    problem = self._precheck(read)
                except Exception as exc:  # noqa: BLE001 - a read; the save is not sent
                    problem = f"the check before saving ({read[2]}) failed ({type(exc).__name__})"
                if problem:
                    return ApiActuationResult(
                        status=ActuationStatus.HALT,
                        substrate=self.substrate,
                        halt_kind=ApiHaltKind.RESPONSE_REJECTED,
                        reason=(
                            f"after the {sent} step write(s) were accepted, {problem}; "
                            "the save was not sent and nothing is retried"
                        ),
                        request_summary=summary,
                    )
        try:
            if prepared.encoding == "json":
                resp = self.transport.send_json(  # type: ignore[attr-defined]
                    prepared.method,
                    prepared.path,
                    prepared.json_text,
                    referer=prepared.referer,
                    headers=dict(prepared.headers),
                    **({"origin": prepared.origin} if prepared.origin else {}),
                )
            elif prepared.encoding == "urlencoded":
                resp = self.transport.post_form(
                    prepared.path, prepared.encoded(), referer=prepared.referer
                )
            else:
                resp = self.transport.post_form(
                    prepared.path,
                    prepared.encoded(),
                    referer=prepared.referer,
                    content_type=prepared.content_type(),
                )
        except Exception as exc:  # noqa: BLE001 - after dispatch began
            self.log.append({"stage": "save", "error": type(exc).__name__})
            return ApiActuationResult(
                status=ActuationStatus.HALT,
                substrate=self.substrate,
                halt_kind=ApiHaltKind.DELIVERY_UNCERTAIN,
                reason=(
                    f"the save request failed after it was sent ({type(exc).__name__}); "
                    "it may have saved, so it is not retried"
                ),
                request_summary=summary,
            )
        accepted = resp.status in (binding.expected_status or list(range(200, 400)))
        signature = True
        if spec.success_pattern:
            target = (resp.headers.get("location") or "") + "\n" + resp.text
            signature = re.search(spec.success_pattern, target) is not None
        if spec.graphql:
            # A GraphQL server answers 200 with the error in the body.
            signature = signature and graphql_succeeded(resp.text)
        self.log.append(
            {
                "stage": "save",
                "status": resp.status,
                "accepted": accepted,
                "signature": signature,
            }
        )
        if accepted and signature:
            return ApiActuationResult(
                status=ActuationStatus.ACTUATED,
                substrate=self.substrate,
                reason=f"{summary} -> {resp.status}",
                http_status=resp.status,
                request_summary=summary,
            )
        return ApiActuationResult(
            status=ActuationStatus.HALT,
            substrate=self.substrate,
            halt_kind=ApiHaltKind.RESPONSE_REJECTED,
            reason=(
                f"{summary} -> {resp.status} without the recorded success response; "
                "it is not retried"
            ),
            http_status=resp.status,
            request_summary=summary,
        )

    @staticmethod
    def build_body(
        spec: Any,
        params: dict[str, str],
        controls: dict[str, Any],
        *,
        enforce_rules: bool = True,
        form: Optional[dict[str, Any]] = None,
    ) -> tuple[list[tuple[str, str]], Optional[str]]:
        body: list[tuple[str, str]] = []
        for field in spec.fields:
            control = controls.get(field.name) or {}
            if field.source in ("submitter", "empty_file"):
                problem = _form_part_problem(field, form)
                if problem:
                    return [], problem
                body.append((field.name, field.value or ""))
                continue
            if field.source == "param":
                body.append((field.name, str(params[field.param])))
            elif field.source == "select_label":
                value = option_for_label(control, str(params[field.param]))
                if value is None:
                    return (
                        [],
                        f"{field.name}: the form has no single option {params[field.param]!r}",
                    )
                body.append((field.name, value))
            elif field.source == "session":
                value = control.get("default") or ""
                if not value:
                    return [], f"{field.name}: the live form carries no session value"
                body.append((field.name, value))
            else:
                if (control.get("default") or "") != field.value:
                    return (
                        [],
                        f"{field.name}: the form's default changed since the recording",
                    )
                body.append((field.name, field.value or ""))
        if enforce_rules:
            sent = dict(body)
            # The browser's own checks before it submits, then the page's
            # validate.js rules: the API step skips neither.
            unchecked = {
                f.name for f in spec.fields if f.source in ("submitter", "empty_file")
            }
            for name, value in body:
                if name in unchecked:
                    continue  # a button's or an empty file input's own part
                problem = native_problem(name, controls.get(name) or {}, value)
                if problem:
                    return [], problem
            for name, rule in (spec.client_rules or {}).items():
                if isinstance(rule, dict):
                    problem = client_rule_problem(name, rule, sent.get(name, ""))
                    if problem:
                        return [], problem
        return body, None


def graphql_succeeded(text: str) -> bool:
    """A GraphQL response with data and no errors."""

    try:
        doc = json.loads(text)
    except ValueError:
        return False
    if isinstance(doc, list) and len(doc) == 1:
        doc = doc[0]  # the answer to a batch of one operation
    return (
        isinstance(doc, dict)
        and not doc.get("errors")
        and isinstance(doc.get("data"), dict)
        and any(v is not None for v in doc["data"].values())
    )


def json_pointer(name: str) -> list[str]:
    """The tokens of a JSON pointer written without its leading ``/``."""

    return [t.replace("~1", "/").replace("~0", "~") for t in name.split("/")]


def fill_json(
    spec: Any, params: dict[str, str]
) -> tuple[str, list[tuple[str, str]], Optional[str]]:
    """``spec.json_body`` with each ``param`` field's leaf set to the run's value.

    A leaf recorded as a number takes the run's value as a number; one that
    isn't a number keeps the save on the GUI. Returns the exact body text, the
    ``(pointer, value)`` rows, and the problem, if any.
    """

    import copy
    import math

    body = copy.deepcopy(spec.json_body)
    rows: list[tuple[str, str]] = []
    for field in spec.fields:
        if field.source != "param" or field.param is None:
            return "", [], f"{field.name}: a JSON save takes only run values"
        tokens = json_pointer(field.name)
        parent: Any = body
        try:
            for token in tokens[:-1]:
                parent = parent[int(token) if isinstance(parent, list) else token]
            key: Any = int(tokens[-1]) if isinstance(parent, list) else tokens[-1]
            leaf = parent[key]
        except (KeyError, IndexError, ValueError, TypeError):
            return "", [], f"{field.name}: not in the recorded body"
        raw = str(params[field.param])
        if not raw:
            # A page often leaves a blank field out of the body altogether,
            # so the recorded shape can't be assumed for it.
            return "", [], f"{field.name}: a blank value may change the body's shape"
        value: Any = raw
        if isinstance(leaf, bool) or leaf is None:
            return "", [], f"{field.name}: the recorded value isn't text or a number"
        if isinstance(leaf, (int, float)):
            try:
                value = int(raw) if re.fullmatch(r"-?\d+", raw) else float(raw)
            except ValueError:
                return "", [], f"{field.name}: {raw!r} is not a number"
            if isinstance(value, float) and not math.isfinite(value):
                return "", [], f"{field.name}: {raw!r} is not a number"
        parent[key] = value
        rows.append((field.name, raw))
    text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return text, rows, None


def _form_part_problem(field: Any, form: Optional[dict[str, Any]]) -> Optional[str]:
    """Why the live form can't send ``field`` (a submitter or an empty file)."""

    if form is None:
        return f"{field.name}: the live form wasn't read"
    if field.source == "submitter":
        if submitter_button(form, field.name, field.value or "") is None:
            return (
                f"{field.name}: the form no longer has exactly one submit button "
                f"that sends {field.value!r}"
            )
        return None
    if field.value:
        return f"{field.name}: the API step never attaches a file"
    if [f["name"] for f in form.get("files") or []].count(field.name) != 1:
        return f"{field.name}: the form no longer has that one file input"
    return None


def _encoding_problem(spec: Any, form: dict[str, Any]) -> Optional[str]:
    """Why the live form would be sent otherwise than the recorded save, or None.

    A form posted as multipart sends every one of its file inputs, so the
    live form must have exactly the recorded ones, and still post as
    multipart unless the page's script sends it (``form_action``).
    """

    files = sorted(f["name"] for f in form.get("files") or [])
    recorded = sorted(f.name for f in spec.fields if f.source == "empty_file")
    if spec.encoding == "multipart":
        if not spec.form_action and form.get("enctype") != "multipart/form-data":
            return "the form no longer posts as multipart/form-data"
        if files != recorded:
            return "the form's file inputs changed since the recording"
    elif files and not spec.form_action:
        return "the form has a file input; the API step doesn't send files"
    return None


class _Missing(KeyError):
    pass


def fill_url(template: str, params: dict[str, str]) -> str:
    """Fill a URL template's ``{param}`` placeholders, URL-encoded.

    A path segment is quoted whole (``/`` included); a query value is
    form-encoded. Anything not a placeholder is kept as recorded. Raises
    ``KeyError`` for a parameter the run doesn't have.
    """

    path, sep, query = template.partition("?")
    path = re.sub(
        r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda m: up.quote(str(_value(params, m.group(1))), safe=""),
        path,
    )
    if not sep:
        return path
    pairs = [
        (k, _fill(v, params)) for k, v in up.parse_qsl(query, keep_blank_values=True)
    ]
    return path + "?" + up.urlencode(pairs)


def _value(params: dict[str, str], key: str) -> str:
    if key not in params:
        raise _Missing(key)
    return str(params[key])


def _fill(template: str, params: dict[str, str]) -> str:
    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in params:
            raise _Missing(key)
        return str(params[key])

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", sub, template)
