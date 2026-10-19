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
from typing import Any, Optional, Protocol

from openadapt_flow.runtime.actuators.api import (
    ActuationStatus,
    ApiActuationResult,
    ApiHaltKind,
)

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
            self._form = {"action": a.get("action", ""), "controls": {}}
            self.forms.append(self._form)
            return
        if self._form is None:
            return
        name = a.get("name")
        if tag == "input" and name:
            kind = a.get("type", "text").lower()
            if kind in ("submit", "button", "image", "reset", "file"):
                return
            if kind in ("checkbox", "radio") and "checked" not in a:
                value: Optional[str] = None
            else:
                value = a.get("value", "on" if kind in ("checkbox", "radio") else "")
            self._form["controls"].setdefault(
                name,
                {"kind": kind, "default": value, "options": None, "id": a.get("id")},
            )
        elif tag == "select" and name:
            self._select = {
                "id": a.get("id"),
                "kind": "select",
                "default": None,
                "options": [],
                "multiple": "multiple" in a,
            }
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
            }
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


def form_controls(html: str, action_path: str) -> Optional[dict[str, Any]]:
    """The named controls of the form in ``html`` that posts to ``action_path``."""

    parser = _FormParser()
    parser.feed(html)
    leaf = up.urlsplit(action_path).path.rsplit("/", 1)[-1]
    for form in parser.forms:
        action = up.urlsplit(form["action"]).path
        if action == action_path or (leaf and action.rsplit("/", 1)[-1] == leaf):
            return form["controls"]
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


def folded_checks_digest(steps: Any) -> str:
    """SHA-256 over the on-screen checks (``expect``) of the folded steps."""

    import hashlib

    payload = [[s.id, [p.model_dump(mode="json") for p in s.expect]] for s in steps]
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

    def post_form(self, path: str, body: str, *, referer: str) -> Response: ...


class PlaywrightSessionTransport:
    """The browser context's own request API: same cookies, same session."""

    def __init__(self, page: Any, *, timeout_s: float = 30.0) -> None:
        parts = up.urlsplit(page.url)
        self.base_url = f"{parts.scheme}://{parts.netloc}"
        self._request = page.context.request
        self._timeout_ms = timeout_s * 1000.0

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

    def post_form(self, path: str, body: str, *, referer: str) -> Response:
        return self._wrap(
            self._request.post(
                self.base_url + path,
                data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": referer,
                    "Origin": self.base_url,
                },
                timeout=self._timeout_ms,
                max_redirects=0,
            )
        )


# -- the prepared save ----------------------------------------------------------


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

    def encoded(self) -> str:
        return up.urlencode(list(self.body))

    def payload_sha256(self) -> str:
        payload = {
            "method": self.method.upper(),
            "path": self.path,
            "content_type": "application/x-www-form-urlencoded",
            "body": self.encoded(),
            "referer": self.referer,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
        try:
            page = self.transport.get(spec.form_path)
            self.log.append({"stage": "read_form", "status": page.status})
            if page.status != 200:
                return unavailable(
                    f"the form page returned HTTP {page.status} (signed out?)"
                )
            controls = form_controls(page.text, binding.url_template)
            if controls is None:
                return unavailable("the form page no longer has the recorded form")
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
            body, problem = self.build_body(spec, params, controls)
            if problem:
                return unavailable(problem)
            for check in spec.prechecks:
                query = [(k, _fill(v, params)) for k, v in check.query]
                qs = "&".join(
                    up.quote(k) + ("=" + up.quote(v) if v else "") for k, v in query
                )
                resp = self.transport.get(check.path + (f"?{qs}" if qs else ""))
                missing = [t for t in check.require_text if t not in resp.text]
                self.log.append(
                    {
                        "stage": "precheck",
                        "path": check.path,
                        "status": resp.status,
                        "missing": missing,
                    }
                )
                if resp.status != 200 or missing:
                    return unavailable(
                        f"the check before saving ({check.from_step or check.path}) did not pass: "
                        f"missing {missing!r}"
                    )
        except KeyError as exc:
            return unavailable(f"the run has no value for {exc}")
        except Exception as exc:  # noqa: BLE001 - only reads were sent
            return unavailable(f"a read before the save failed ({type(exc).__name__})")
        return PreparedSave(
            method=binding.method,
            path=binding.url_template,
            body=tuple(body),
            referer=self.transport.base_url + spec.form_path,
            session_fields=frozenset(
                f.name for f in spec.fields if f.source == "session"
            ),
        )

    def send(self, binding: Any, prepared: "PreparedSave") -> ApiActuationResult:
        """Send exactly ``prepared``, once. Never retried."""

        spec = binding.web_form
        summary = f"{binding.method} {binding.url_template}"
        try:
            resp = self.transport.post_form(
                prepared.path, prepared.encoded(), referer=prepared.referer
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
    ) -> tuple[list[tuple[str, str]], Optional[str]]:
        body: list[tuple[str, str]] = []
        for field in spec.fields:
            control = controls.get(field.name) or {}
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
            for name, rule in (spec.client_rules or {}).items():
                if (
                    isinstance(rule, dict)
                    and rule.get("presence")
                    and not sent.get(name)
                ):
                    return [], f"{name} is required by the form and would be empty"
        return body, None


class _Missing(KeyError):
    pass


def _fill(template: str, params: dict[str, str]) -> str:
    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in params:
            raise _Missing(key)
        return str(params[key])

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", sub, template)
