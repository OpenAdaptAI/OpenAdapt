"""Spike: compile a recorded browser form save into an API step, and run it.

Two halves, both model-free:

* :func:`compile_from_har` reads the HAR captured while the compiled GUI
  program replayed one referral, finds the requests that did the work (sign-in,
  the duplicate check, the save), and explains every field of the save body by
  where its value came from: a recorded parameter, a select option chosen by a
  recorded label, a server-issued session value (the CSRF token), or the
  form's own default. A field it cannot explain stops the compile; that
  workflow stays on the GUI.
* :class:`SessionFormActuator` implements the engine's ``ApiActuator``
  protocol (``actuate(binding, params) -> ApiActuationResult``) for a web
  app's own form endpoints: it signs in with a fresh session, fetches the form
  page for the session's CSRF token and current defaults, refuses before
  sending anything if the form changed since the recording, runs the
  compiled duplicate check, then posts the save once. The engine's API tier
  then confirms the write against the system of record with the same
  ``EffectVerifier`` the GUI path uses.

This is spike code for one application (OpenEMR 8.0.0.3) on synthetic data.
"""

from __future__ import annotations

import json
import re
import urllib.parse as up
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

# -- HTML form parsing ---------------------------------------------------------


class _FormParser(HTMLParser):
    """Named controls of each <form>: defaults and select options."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, Any]] = []
        self._form: dict[str, Any] | None = None
        self._select: dict[str, Any] | None = None
        self._option: dict[str, Any] | None = None
        self._textarea: dict[str, Any] | None = None
        self._nested = 0  # <form> start tags ignored inside an open form

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v if v is not None else "") for k, v in attrs}
        if tag == "form":
            if self._form is not None:
                # HTML parsing ignores a <form> inside an open form; its
                # controls belong to (and are posted with) the outer form.
                self._nested += 1
                return
            self._form = {"action": a.get("action", ""), "name": a.get("name", ""), "controls": {}}
            self.forms.append(self._form)
            return
        if self._form is None:
            return
        name = a.get("name")
        if tag == "input" and name:
            kind = a.get("type", "text").lower()
            if kind in ("checkbox", "radio") and "checked" not in a:
                value = None
            elif kind in ("submit", "button", "image", "reset", "file"):
                return
            else:
                value = a.get("value", "on" if kind in ("checkbox", "radio") else "")
            self._form["controls"].setdefault(name, {"kind": kind, "default": value, "options": None})
        elif tag == "select" and name:
            self._select = {"kind": "select", "default": None, "options": [], "multiple": "multiple" in a}
            self._form["controls"].setdefault(name, self._select)
        elif tag == "option" and self._select is not None:
            self._option = {"value": a.get("value"), "label": "", "selected": "selected" in a}
            self._select["options"].append(self._option)
        elif tag == "textarea" and name:
            self._textarea = {"kind": "textarea", "default": "", "options": None}
            self._form["controls"].setdefault(name, self._textarea)

    def handle_data(self, data: str) -> None:
        if self._option is not None:
            self._option["label"] += data
        elif self._textarea is not None:
            self._textarea["default"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._option is not None:
            o = self._option
            o["label"] = o["label"].strip()
            if o["value"] is None:
                o["value"] = o["label"]
            self._option = None
        elif tag == "select" and self._select is not None:
            opts = self._select["options"]
            chosen = [o for o in opts if o["selected"]] or ([] if self._select["multiple"] else opts[:1])
            self._select["default"] = chosen[0]["value"] if chosen else ""
            self._select = None
        elif tag == "textarea":
            self._textarea = None
        elif tag == "form":
            if self._nested:
                self._nested -= 1
            else:
                self._form = None


def parse_form(html: str, action_suffix: str) -> dict[str, Any]:
    p = _FormParser()
    p.feed(html)
    for form in p.forms:
        if form["action"].split("?")[0].endswith(action_suffix):
            return form["controls"]
    raise ValueError(f"no <form> posting to {action_suffix}")


_CONSTRAINTS = re.compile(r"var constraints = (\{.*?\});")


def client_rules(html: str) -> dict[str, Any] | None:
    """The page's client-side validation rules (OpenEMR's ``constraints``).

    The browser enforces these before it submits; a direct POST skips them,
    so the API step records them, refuses if they change, and enforces them.
    """
    m = _CONSTRAINTS.search(html)
    return json.loads(m.group(1)) if m else None


def option_value_for_label(control: dict[str, Any], label: str) -> str | None:
    hits = [o["value"] for o in (control.get("options") or []) if o["label"] == label]
    return hits[0] if len(hits) == 1 else None


# -- compiler ------------------------------------------------------------------

_STATIC = re.compile(r"\.(js|css|png|svg|gif|ico|woff2?|ttf|map)(\?|$)")


def _entries(har: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in har["log"]["entries"] if not _STATIC.search(e["request"]["url"])]


def _path(url: str) -> str:
    return up.urlsplit(url).path


def _body_pairs(entry: dict[str, Any]) -> list[tuple[str, str]]:
    return up.parse_qsl(entry["request"].get("postData", {}).get("text") or "", keep_blank_values=True)


def compile_from_har(har_path: Path, gui_bundle: Path, out_dir: Path,
                     run_params: dict[str, str] | None = None) -> dict[str, Any]:
    """Compile the save of a recorded GUI workflow into one API step.

    ``run_params`` are the values the captured run used (default: the
    bundle's recorded demonstration values).

    Returns the compile report (also written to ``out_dir``). Raises
    ``CompileRefused`` when any field cannot be explained.
    """
    from openadapt_flow.ir import ApiBinding, Workflow

    har = json.loads(Path(har_path).read_text())
    wf = Workflow.load(gui_bundle)
    params = dict(run_params or wf.params)
    if sorted(params) != sorted(wf.params):
        raise CompileRefused("run params do not match the workflow's parameters")
    entries = _entries(har)

    def find(method: str, suffix: str) -> list[dict[str, Any]]:
        return [e for e in entries if e["request"]["method"] == method and _path(e["request"]["url"]).endswith(suffix)]

    # The save is the one form POST whose body carries the recorded values.
    posts = [e for e in entries if e["request"]["method"] == "POST"
             and "x-www-form-urlencoded" in (e["request"].get("postData", {}).get("mimeType") or "")]
    scored = sorted(posts, key=lambda e: -sum(v in params.values() for _, v in _body_pairs(e) if v))
    save = scored[0]
    save_url = save["request"]["url"]
    hits = sum(v in params.values() for _, v in _body_pairs(save) if v)
    if hits < len(params) // 2:
        raise CompileRefused("no form POST carries the recorded parameter values")

    # The form page the save posts from: the last GET whose HTML has a form
    # with that action, before the save.
    save_i = har["log"]["entries"].index(save)
    form_page = None
    for e in har["log"]["entries"][:save_i]:
        t = e["response"].get("content", {}).get("text") or ""
        if e["request"]["method"] == "GET" and "<form" in t and _path(save_url).rsplit("/", 1)[-1] in t:
            try:
                controls = parse_form(t, _path(save_url).rsplit("/", 1)[-1])
            except ValueError:
                continue
            form_page = (e, controls)
    if form_page is None:
        raise CompileRefused("the form page the save posts from is not in the capture")
    form_entry, controls = form_page
    rules = client_rules(form_entry["response"]["content"]["text"])

    # Values the server issued to this session (seen in responses before the
    # save and not typed by the user): CSRF tokens and the like.
    session_texts = "\n".join((e["response"].get("content", {}).get("text") or "")
                              for e in har["log"]["entries"][:save_i])

    by_value: dict[str, list[str]] = {}
    for k, v in params.items():
        by_value.setdefault(v, []).append(k)

    fields: list[dict[str, Any]] = []
    body: dict[str, Any] = {}
    for name, value in _body_pairs(save):
        ctl = controls.get(name)
        rec: dict[str, Any] = {"name": name, "recorded": value}
        if value and value in by_value and len(by_value[value]) == 1:
            rec.update(source="param", param=by_value[value][0])
            body[name] = "{" + by_value[value][0] + "}"
        elif value and ctl and ctl.get("options"):
            label = next((o["label"] for o in ctl["options"] if o["value"] == value), None)
            if label and label in by_value and len(by_value[label]) == 1:
                rec.update(source="select_label", param=by_value[label][0], label=label)
                body[name] = "{" + by_value[label][0] + "}"
            elif ctl.get("default") == value:
                rec.update(source="form_default")
                body[name] = value
            else:
                raise CompileRefused(f"field {name!r}: select value not explained")
        elif ctl is not None and ctl.get("kind") == "hidden" and value and value in session_texts and value == ctl.get("default"):
            rec.update(source="session", recorded="<session value, not stored>")
            body[name] = "{__session_" + name + "}"
        elif ctl is not None and (ctl.get("default") or "") == value:
            rec.update(source="form_default")
            body[name] = value
        else:
            raise CompileRefused(f"field {name!r}: value not explained by params, options, session or defaults")
        fields.append(rec)

    # Duplicate check: the GET the form's script opened right before the save,
    # with the recorded values in its query; its pass condition is the GUI
    # step's own on-screen expectation.
    precheck = None
    gui_dialog = next((s for s in wf.steps if s.expect and any(
        getattr(p, "text", None) == "No matches were found." for p in s.expect)), None)
    for e in reversed(har["log"]["entries"][:save_i]):
        if e["request"]["method"] != "GET":
            continue
        q = up.parse_qsl(up.urlsplit(e["request"]["url"]).query, keep_blank_values=True)
        if sum(v in by_value for _, v in q if v) >= 2:
            qt = []
            for k, v in q:
                qt.append([k, "{" + by_value[v][0] + "}" if v in by_value and len(by_value[v]) == 1 else v])
            precheck = {
                "method": "GET", "path": _path(e["request"]["url"]), "query": qt,
                "require_text": [p.text for p in gui_dialog.expect] if gui_dialog else [],
                "from_gui_step": gui_dialog.id if gui_dialog else None,
            }
            break

    # Sign-in: the form POST that set the session cookie. Secrets are never
    # copied from the capture; the deployment supplies them at run time.
    login = None
    for e in entries:
        if e["request"]["method"] != "POST":
            continue
        pairs = _body_pairs(e)
        if any("pass" in k.lower() for k, _ in pairs) and any(
                h["name"].lower() == "set-cookie" for h in e["response"]["headers"]):
            lb = []
            for k, v in pairs:
                if "pass" in k.lower():
                    lb.append([k, "{__secret_password}"])
                elif k.lower() in ("authuser", "username", "user", "login"):
                    lb.append([k, "{__secret_username}"])
                else:
                    lb.append([k, v])
            loc = next((h["value"] for h in e["response"]["headers"] if h["name"].lower() == "location"), "")
            login = {"method": "POST", "path": _path(e["request"]["url"]),
                     "query": up.urlsplit(e["request"]["url"]).query, "body": lb,
                     "expect_status": e["response"]["status"], "expect_location_path": _path(loc)}
            break
    if login is None:
        raise CompileRefused("no sign-in request in the capture")

    resp_text = save["response"].get("content", {}).get("text") or ""
    m = re.search(r"set_pid=(\d+)", resp_text)
    success = re.escape(resp_text[m.start():m.end()]).replace(re.escape(m.group(1)), r"(\d+)") if m else None

    save_step = next(s for s in wf.steps if s.effects)
    binding = ApiBinding(
        kind="rest",
        on_unavailable="halt",
        method="POST",
        url_template=_path(save_url),
        body_template=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        expected_status=[save["response"]["status"]],
        timeout_s=30.0,
        effects=list(save_step.effects),
    )
    api_step = save_step.model_copy(update={
        "id": "api_save",
        "intent": f"save the form via {binding.method} {binding.url_template} (folds {len(wf.steps)} GUI steps)",
        "api_binding": binding,
        "expect": [],
    })
    api_wf = wf.model_copy(update={"steps": [api_step], "name": wf.name + " (api tier)",
                                   "interstitials": [], "program": None, "subflows": {}})
    tier = {
        "schema": "openadapt.spike.session_form/1",
        "login": login,
        "form_page": {"path": _path(form_entry["request"]["url"]),
                      "action": _path(save_url).rsplit("/", 1)[-1],
                      "controls": sorted(controls),
                      "client_rules": rules},
        "precheck": precheck,
        "success_regex": success,
        "fields": fields,
        "folded_gui_steps": [s.id for s in wf.steps],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    api_wf.save(out_dir)
    (out_dir / "session_form.json").write_text(json.dumps(tier, indent=1) + "\n")
    counts: dict[str, int] = {}
    for f in fields:
        counts[f["source"]] = counts.get(f["source"], 0) + 1
    report = {"save": {"method": "POST", "path": _path(save_url), "fields": len(fields), "by_source": counts},
              "precheck": precheck, "login": {"path": login["path"], "fields": [k for k, _ in login["body"]]},
              "success_regex": success, "param_fields": {f["name"]: f.get("param") for f in fields if f.get("param")}}
    (out_dir / "compile_report.json").write_text(json.dumps(report, indent=1) + "\n")
    return report


class CompileRefused(Exception):
    """The capture cannot be compiled to the API tier; keep the GUI program."""


# -- runtime -------------------------------------------------------------------


class SessionFormActuator:
    """Run a compiled session-form save: sign in, re-read the form, check, post.

    Implements the engine's API actuator protocol. Every refusal before the
    save POST is ``UNAVAILABLE`` (nothing was sent that writes); anything after
    the POST began follows ``ApiActuator``'s no-double-write rule (``HALT``).
    """

    substrate = "rest"

    def __init__(self, base_url: str, tier: dict[str, Any], *, username: str, password: str,
                 timeout_s: float = 30.0) -> None:
        import requests

        self.base_url = base_url.rstrip("/")
        self.tier = tier
        self.secrets = {"__secret_username": username, "__secret_password": password}
        self.session = requests.Session()
        self.timeout_s = timeout_s
        self.marks: dict[str, float] = {}
        self.log: list[dict[str, Any]] = []

    # Each pre-save stage returns an error string or None.

    def sign_in(self) -> str | None:
        import time

        lg = self.tier["login"]
        data = [(k, v.format_map(self.secrets) if "{" in v else v) for k, v in lg["body"]]
        url = f"{self.base_url}{lg['path']}" + (f"?{lg['query']}" if lg["query"] else "")
        self.session.get(self.base_url + "/", timeout=self.timeout_s)  # App cookie, as the browser does
        r = self.session.post(url, data=data, allow_redirects=False, timeout=self.timeout_s)
        loc = up.urlsplit(r.headers.get("location", "")).path
        self.log.append({"stage": "sign_in", "status": r.status_code, "location_path": loc})
        self.marks["signed_in"] = time.time()
        if r.status_code != lg["expect_status"] or loc != lg["expect_location_path"]:
            return f"sign-in did not reach {lg['expect_location_path']} (got {r.status_code} {loc})"
        return None

    def read_form(self) -> tuple[dict[str, Any] | None, str | None]:
        import time

        fp = self.tier["form_page"]
        r = self.session.get(self.base_url + fp["path"], timeout=self.timeout_s)
        self.marks["form_read"] = time.time()
        self.log.append({"stage": "read_form", "status": r.status_code, "bytes": len(r.content)})
        if r.status_code != 200:
            return None, f"form page returned {r.status_code}"
        try:
            controls = parse_form(r.text, fp["action"])
        except ValueError:
            return None, "form page has no save form (session or page changed)"
        if client_rules(r.text) != fp.get("client_rules"):
            return None, "the form's client-side validation rules changed since the recording"
        if sorted(controls) != fp["controls"]:
            added = sorted(set(controls) - set(fp["controls"]))
            removed = sorted(set(fp["controls"]) - set(controls))
            return None, f"form changed since the recording (added {added[:5]}, removed {removed[:5]})"
        return controls, None

    def run_precheck(self, params: dict[str, str]) -> str | None:
        import time

        pc = self.tier.get("precheck")
        if not pc:
            return None
        q = [(k, v.format_map(params) if "{" in v else v) for k, v in pc["query"]]
        qs = "&".join(up.quote(k) + ("=" + up.quote(v) if v else "") for k, v in q)
        r = self.session.get(f"{self.base_url}{pc['path']}?{qs}", timeout=self.timeout_s)
        self.marks["prechecked"] = time.time()
        missing = [t for t in pc["require_text"] if t not in r.text]
        self.log.append({"stage": "precheck", "status": r.status_code, "missing": missing})
        if r.status_code != 200 or missing:
            return f"duplicate check did not pass: missing {missing!r} (HTTP {r.status_code})"
        return None

    def build_body(self, binding: Any, params: dict[str, str], controls: dict[str, Any], *,
                   enforce_rules: bool = True) -> tuple[list[tuple[str, str]] | None, str | None]:
        body: list[tuple[str, str]] = []
        by_name = {f["name"]: f for f in self.tier["fields"]}
        for name, template in binding.body_template.items():
            f = by_name[name]
            ctl = controls.get(name) or {}
            src = f["source"]
            if src == "param":
                body.append((name, params[f["param"]]))
            elif src == "select_label":
                value = option_value_for_label(ctl, params[f["param"]])
                if value is None:
                    return None, f"{name}: no single option labelled {params[f['param']]!r}"
                body.append((name, value))
            elif src == "session":
                body.append((name, ctl.get("default") or ""))
                if not body[-1][1]:
                    return None, f"{name}: the fresh form carries no session value"
            elif src == "form_default":
                if (ctl.get("default") or "") != template:
                    return None, f"{name}: form default changed since the recording"
                body.append((name, template))
        # Enforce the recorded client-side rules the browser would have applied.
        sent = dict(body)
        rules = (self.tier["form_page"].get("client_rules") or {}) if enforce_rules else {}
        for name, rule in rules.items():
            if rule.get("presence") and not sent.get(name):
                return None, f"{name} is required by the form and would be empty"
        return body, None

    def actuate(self, binding: Any, params: dict[str, str]) -> Any:
        import time

        import requests

        from openadapt_flow.runtime.actuators import (
            ActuationStatus,
            ApiActuationResult,
            ApiHaltKind,
        )

        summary = f"{binding.method} {binding.url_template}"

        def unavailable(reason: str) -> Any:
            return ApiActuationResult(status=ActuationStatus.UNAVAILABLE, reason=reason + " -- save not sent",
                                      request_summary=summary)

        try:
            err = self.sign_in()
            if err:
                return unavailable(err)
            controls, err = self.read_form()
            if err:
                return unavailable(err)
            body, err = self.build_body(binding, params, controls)
            if err:
                return unavailable(err)
            err = self.run_precheck(params)
            if err:
                return unavailable(err)
        except (KeyError, requests.RequestException) as exc:
            # Only sign-in, a page read and the read-only duplicate search were
            # sent; none of them writes the record.
            return unavailable(f"pre-save stage failed ({type(exc).__name__})")

        self.marks["save_sent"] = time.time()
        try:
            r = self.session.post(self.base_url + binding.url_template, data=body,
                                  headers={"Referer": self.base_url + self.tier["form_page"]["path"]},
                                  allow_redirects=False, timeout=binding.timeout_s or self.timeout_s)
        except Exception as exc:  # noqa: BLE001 - after dispatch began
            return ApiActuationResult(status=ActuationStatus.HALT, halt_kind=ApiHaltKind.DELIVERY_UNCERTAIN,
                                      reason=f"save transport failed after dispatch ({type(exc).__name__})",
                                      request_summary=summary)
        self.marks["save_done"] = time.time()
        sig = self.tier.get("success_regex")
        ok = r.status_code in (binding.expected_status or [200]) and (sig is None or re.search(sig, r.text))
        self.log.append({"stage": "save", "status": r.status_code, "success_signature": bool(ok)})
        if ok:
            return ApiActuationResult(status=ActuationStatus.ACTUATED, reason=f"{summary} -> {r.status_code}",
                                      http_status=r.status_code, request_summary=summary)
        return ApiActuationResult(status=ActuationStatus.HALT, halt_kind=ApiHaltKind.RESPONSE_REJECTED,
                                  reason=f"{summary} -> {r.status_code} without the recorded success response",
                                  http_status=r.status_code, request_summary=summary)
