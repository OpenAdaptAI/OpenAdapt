"""Stateful Multi-Page Zero-PHI Digital Twin Synthesizer and Offline TDD Self-Healer."""

from __future__ import annotations

import fnmatch
import html
import inspect
import json
import re
import tempfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple, Union
from urllib.parse import parse_qs, urlparse

from openadapt_kernel.digital_twin import _CanaryProjector
from openadapt_kernel.egress_guard import (
    EgressBlockedError,
    EgressPolicy,
    LocalEgressGuard,
    summarize_egress_ledger,
)
from openadapt_kernel.macro import observe_schema
from openadapt_kernel.zero_phi_repair import (
    DistilledRepairStep,
    _SymVal,
    _ZeroPhiUIProxy,
    export_zero_phi_dom_twin,
    sanitize_ui_label_allowlist,
    verify_symbolic_repair_ast,
)

_TWIN_EXTRA_TOKENS: FrozenSet[str] = frozenset(
    "api collection count get href last_record location multipage oa patch path put rec redirect response route routes store twin".split()
)

_SKIP_CONTENT_TAGS: FrozenSet[str] = frozenset(
    {"script", "style", "img", "canvas", "svg", "video", "audio", "iframe", "object", "embed", "picture", "source", "noscript"}
)
_SAFE_TAGS: FrozenSet[str] = frozenset(
    "a br button dd dialog div dl dt em fieldset footer form h1 h2 h3 h4 h5 h6 "
    "header hr input label legend li main nav ol optgroup option p section select "
    "small span strong table tbody td textarea tfoot th thead tr ul".split()
)
_VOID_TAGS: FrozenSet[str] = frozenset({"br", "hr", "input"})
_SAFE_INPUT_TYPES: FrozenSet[str] = frozenset(
    {"text", "checkbox", "radio", "submit", "button", "number", "date", "email", "tel", "password", "search"}
)
_SAFE_ROLES: FrozenSet[str] = frozenset(
    {"dialog", "button", "textbox", "combobox", "checkbox", "radio", "listbox", "option", "menu", "menuitem", "form", "main", "region", "alert"}
)
_SAFE_FORM_METHODS: FrozenSet[str] = frozenset({"get", "post"})
_SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9_/-]*$")
_STORE_MARKER_RE = re.compile(
    r'(<div\b[^>]*\bdata-oa-twin-store="([A-Za-z0-9_]+)"[^>]*>)(</div>)',
    re.IGNORECASE,
)


def _default_twin_policy(policy: Optional[EgressPolicy] = None) -> EgressPolicy:
    if policy is None:
        return EgressPolicy(extra_allowed_tokens=_TWIN_EXTRA_TOKENS)
    return EgressPolicy(
        allowed_tokens=policy.allowed_tokens,
        include_html_aria_vocab=policy.include_html_aria_vocab,
        include_clinical_vocab=policy.include_clinical_vocab,
        extra_allowed_tokens=policy.extra_allowed_tokens | _TWIN_EXTRA_TOKENS,
        allowed_var_keys=policy.allowed_var_keys,
    )


def _sanitize_url_path(raw_path: str, vocab: FrozenSet[str]) -> str:
    parsed = urlparse(raw_path or "/").path or "/"
    parts = [seg for seg in parsed.split("/") if seg]
    safe_parts: List[str] = []
    for seg in parts:
        low = seg.lower()
        if low in vocab:
            safe_parts.append(low)
        else:
            safe_parts.append("item")
    return "/" + "/".join(safe_parts)


class _ZeroPhiHTMLSanitizer(HTMLParser):
    """Sanitize raw HTML into a zero-PHI runnable structural twin while preserving forms and links."""

    def __init__(self, projector: _CanaryProjector, vocab: FrozenSet[str]) -> None:
        super().__init__(convert_charrefs=True)
        self._projector = projector
        self._vocab = vocab
        self._skip_depth = 0
        self._in_textarea = False
        self._out: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        low = tag.lower()
        if low in _SKIP_CONTENT_TAGS:
            if low not in ("img", "source", "embed"):
                self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if low in ("html", "head", "body", "meta", "title"):
            return
        safe_tag = low if low in _SAFE_TAGS else "div"
        attr_map = {k.lower(): ("" if v is None else v) for k, v in attrs}
        rendered: List[str] = []

        if safe_tag == "input":
            itype = attr_map.get("type", "text").lower()
            rendered.append(f'type="{itype if itype in _SAFE_INPUT_TYPES else "text"}"')
        elif safe_tag == "button":
            btype = attr_map.get("type", "button").lower()
            if btype in ("button", "submit", "reset"):
                rendered.append(f'type="{btype}"')
        elif safe_tag == "form":
            method = attr_map.get("method", "post").lower()
            if method in _SAFE_FORM_METHODS:
                rendered.append(f'method="{method}"')
            if "action" in attr_map:
                action_path = _sanitize_url_path(attr_map["action"], self._vocab)
                rendered.append(f'action="{action_path}"')
        elif safe_tag == "a" and "href" in attr_map:
            href_path = _sanitize_url_path(attr_map["href"], self._vocab)
            rendered.append(f'href="{href_path}"')

        if "name" in attr_map and safe_tag in ("input", "select", "textarea", "button"):
            raw_name = attr_map["name"].strip().lower()
            if raw_name in self._vocab:
                rendered.append(f'name="{raw_name}"')
            else:
                rendered.append(f'name="{self._projector.project(raw_name) or "field"}"')

        role = attr_map.get("role", "").lower()
        if role in _SAFE_ROLES:
            rendered.append(f'role="{role}"')

        oa_idx = attr_map.get("data-oa-idx", "")
        if oa_idx.isdigit() and len(oa_idx) <= 4:
            rendered.append(f'data-oa-idx="{oa_idx}"')

        store_col = attr_map.get("data-oa-twin-store", "").strip().lower()
        if store_col and store_col in self._vocab:
            rendered.append(f'data-oa-twin-store="{store_col}"')

        for bool_attr in ("disabled", "required", "checked", "selected"):
            if bool_attr in attr_map:
                rendered.append(bool_attr)

        for aria_bool in ("aria-required", "aria-disabled", "aria-checked", "aria-expanded", "aria-selected"):
            val = attr_map.get(aria_bool, "").lower()
            if val in ("true", "false"):
                rendered.append(f'{aria_bool}="{val}"')

        for text_attr in ("placeholder", "aria-label"):
            if text_attr in attr_map:
                proj = self._projector.project(attr_map[text_attr])
                if proj:
                    rendered.append(f'{text_attr}="{html.escape(proj, quote=True)}"')

        if safe_tag == "option":
            raw_val = attr_map.get("value", "")
            if raw_val == "":
                rendered.append('value=""')
            else:
                proj_val = self._projector.project(raw_val)
                rendered.append(f'value="{html.escape(proj_val, quote=True)}"')

        attr_str = (" " + " ".join(rendered)) if rendered else ""
        self._out.append(f"<{safe_tag}{attr_str}>")
        if safe_tag == "textarea":
            self._in_textarea = True

    def handle_endtag(self, tag: str) -> None:
        low = tag.lower()
        if low in _SKIP_CONTENT_TAGS:
            if low not in ("img", "source", "embed") and self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return
        if low in ("html", "head", "body", "meta", "title"):
            return
        safe_tag = low if low in _SAFE_TAGS else "div"
        if safe_tag == "textarea":
            self._in_textarea = False
        if safe_tag not in _VOID_TAGS:
            self._out.append(f"</{safe_tag}>")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0 or self._in_textarea:
            return
        if not data or not data.strip():
            return
        projected = self._projector.project(data)
        if projected:
            self._out.append(html.escape(projected, quote=False))

    def get_sanitized_html(self) -> str:
        body = "".join(self._out)
        return (
            '<!doctype html><html><head><meta charset="utf-8"><title>Twin</title></head>'
            f"<body>{body}</body></html>"
        )


def _sanitize_template_value(obj: Any, projector: _CanaryProjector, vocab: FrozenSet[str]) -> Any:
    if isinstance(obj, str):
        if obj in ("{{collection}}", "{{last_record}}", "{{count}}"):
            return obj
        if _SAFE_PATH_RE.fullmatch(obj):
            return _sanitize_url_path(obj, vocab)
        return projector.project(obj)
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            sk = k if str(k).lower() in vocab else projector.project(str(k))
            out[sk] = _sanitize_template_value(v, projector, vocab)
        return out
    if isinstance(obj, list):
        return [_sanitize_template_value(item, projector, vocab) for item in obj]
    return obj


@dataclass(frozen=True)
class TwinRouteSpec:
    """Declarative stateful HTTP route specification for an offline Digital Twin."""

    method: str
    path_pattern: str
    response_template: Any = field(default_factory=lambda: {"status": "ok"})
    mutates_collection: str = ""
    status_code: int = 200
    redirect_to: str = ""

    def matches(self, method: str, path: str) -> bool:
        if self.method.upper() != method.upper():
            return False
        return self.path_pattern == path or fnmatch.fnmatch(path, self.path_pattern)


@dataclass(frozen=True)
class TwinPageSpec:
    """Declarative page specification for a multi-page Zero-PHI Digital Twin."""

    url_path: str
    sanitized_html: str
    route_specs: Tuple[TwinRouteSpec, ...] = ()


class StatefulTwinApp:
    """In-memory multi-page offline web app with Playwright route interception and egress audit."""

    def __init__(
        self,
        pages: Dict[str, TwinPageSpec],
        route_specs: Sequence[TwinRouteSpec],
        egress_guard: LocalEgressGuard,
        *,
        base_url: str = "http://twin.local",
        canary_counts: Optional[Dict[str, int]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.pages: Dict[str, TwinPageSpec] = dict(pages)
        self.route_specs: List[TwinRouteSpec] = list(route_specs)
        self.egress_guard = egress_guard
        self.canary_counts: Dict[str, int] = dict(canary_counts or {})
        self.store: Dict[str, List[Dict[str, Any]]] = {}
        self.live_network_requests_attempted: int = 0
        self.handled_twin_requests: List[Dict[str, Any]] = []

    def reset_state(self) -> None:
        """Reset in-memory collections and request counters between offline TDD turns."""
        self.store.clear()
        self.live_network_requests_attempted = 0
        self.handled_twin_requests.clear()

    def _parse_request_payload(self, request: Any) -> Dict[str, Any]:
        raw = request.post_data or ""
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
            return {"value": parsed}
        except json.JSONDecodeError:
            qs = parse_qs(raw, keep_blank_values=True)
            return {k: (v[0] if len(v) == 1 else v) for k, v in qs.items()}

    def _render_template(self, template: Any, collection: str, last_record: Optional[Dict[str, Any]]) -> Any:
        items = list(self.store.get(collection, [])) if collection else []
        if isinstance(template, str):
            if template == "{{collection}}":
                return items
            if template == "{{last_record}}":
                return last_record or {}
            if template == "{{count}}":
                return len(items)
            return template
        if isinstance(template, dict):
            return {k: self._render_template(v, collection, last_record) for k, v in template.items()}
        if isinstance(template, list):
            return [self._render_template(v, collection, last_record) for v in template]
        return template

    def _render_page_html(self, raw_html: str) -> str:
        def _inject_store(match: re.Match[str]) -> str:
            open_tag, col_name, close_tag = match.group(1), match.group(2), match.group(3)
            items = self.store.get(col_name, [])
            if not items:
                return f"{open_tag}{close_tag}"
            lis = []
            for item in items:
                text = " ".join(f"{k}: {v}" for k, v in sorted(item.items()))
                lis.append(f"<li>{html.escape(text, quote=False)}</li>")
            return f"{open_tag}<ul>{''.join(lis)}</ul>{close_tag}"

        return _STORE_MARKER_RE.sub(_inject_store, raw_html)

    def _handle_route(self, route: Any) -> None:
        req = route.request
        url = str(req.url)
        if not url.startswith(self.base_url):
            self.live_network_requests_attempted += 1
            route.abort("blockedbyclient")
            return

        method = str(req.method).upper()
        path = urlparse(url).path or "/"

        for spec in self.route_specs:
            if spec.matches(method, path):
                last_rec: Optional[Dict[str, Any]] = None
                if spec.mutates_collection and method in ("POST", "PUT", "PATCH"):
                    payload = self._parse_request_payload(req)
                    bucket = self.store.setdefault(spec.mutates_collection, [])
                    last_rec = {"id": f"rec_{len(bucket) + 1}", **payload}
                    bucket.append(last_rec)
                elif spec.mutates_collection and method == "DELETE":
                    self.store[spec.mutates_collection] = []

                self.handled_twin_requests.append(
                    {"method": method, "path": path, "status": spec.status_code, "collection": spec.mutates_collection}
                )
                if spec.redirect_to:
                    target_page = self.pages.get(spec.redirect_to)
                    if target_page is not None:
                        rendered_target = self._render_page_html(target_page.sanitized_html)
                        nav_script = f"<script>window.history.replaceState(null, '', {json.dumps(spec.redirect_to)});</script>"
                        body_html = rendered_target.replace("</body>", f"{nav_script}</body>")
                        route.fulfill(
                            status=200,
                            content_type="text/html; charset=utf-8",
                            body=body_html,
                        )
                        return
                    target = self.base_url + spec.redirect_to
                    route.fulfill(
                        status=303 if spec.status_code == 200 else spec.status_code,
                        headers={"Location": target},
                        body="",
                    )
                    return

                rendered = self._render_template(spec.response_template, spec.mutates_collection, last_rec)
                if isinstance(rendered, str):
                    route.fulfill(
                        status=spec.status_code,
                        content_type="text/html; charset=utf-8",
                        body=self._render_page_html(rendered),
                    )
                else:
                    route.fulfill(
                        status=spec.status_code,
                        content_type="application/json",
                        body=json.dumps(rendered),
                    )
                return

        page_spec = self.pages.get(path)
        if page_spec is not None and method == "GET":
            self.handled_twin_requests.append({"method": method, "path": path, "status": 200, "collection": ""})
            route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=self._render_page_html(page_spec.sanitized_html),
            )
            return

        self.handled_twin_requests.append({"method": method, "path": path, "status": 404, "collection": ""})
        route.fulfill(status=404, content_type="text/plain; charset=utf-8", body="not found")

    def install_on_page(self, page: Any) -> None:
        """Install offline route interception on a Playwright page (0 live network requests)."""
        page.route("**/*", self._handle_route)

    def egress_audit(self) -> Dict[str, Any]:
        """Return cryptographic egress audit summary for this twin."""
        entries = self.egress_guard.read_ledger()
        summary = summarize_egress_ledger(entries)
        return {
            **summary,
            "sha256_chain_integrity_verified": self.egress_guard.verify_ledger_integrity(),
            "entry_sha256s": [str(e.get("entry_sha256", "")) for e in entries],
            "canary_counts": dict(self.canary_counts),
        }


def synthesize_zero_phi_stateful_twin(
    pages: Sequence[TwinPageSpec],
    *,
    api_routes: Sequence[TwinRouteSpec] = (),
    egress_policy: Optional[EgressPolicy] = None,
    ledger_path: Optional[Union[str, Path]] = None,
    base_url: str = "http://twin.local",
) -> StatefulTwinApp:
    """Strip all PHI/PII from multi-page HTML and API templates and verify via LocalEgressGuard."""
    policy = _default_twin_policy(egress_policy)
    vocab = policy.effective_tokens()
    projector = _CanaryProjector(vocab)

    if ledger_path is None:
        tmp_dir = Path(tempfile.mkdtemp(prefix="oa_stateful_twin_"))
        ledger_file = tmp_dir / "outbound_egress_ledger.jsonl"
    else:
        ledger_file = Path(ledger_path)
    guard = LocalEgressGuard(ledger_file, policy=policy)

    sanitized_pages: Dict[str, TwinPageSpec] = {}
    all_routes: List[TwinRouteSpec] = []

    def _sanitize_route(spec: TwinRouteSpec) -> TwinRouteSpec:
        safe_path = _sanitize_url_path(spec.path_pattern, vocab)
        safe_col = spec.mutates_collection.lower() if spec.mutates_collection.lower() in vocab else ""
        safe_redirect = _sanitize_url_path(spec.redirect_to, vocab) if spec.redirect_to else ""
        safe_tpl = _sanitize_template_value(spec.response_template, projector, vocab)
        verdict = guard.check_and_record(
            {"method": spec.method.upper(), "path": safe_path, "response": safe_tpl},
            destination="stateful_twin_route",
        )
        if not verdict.allowed:
            raise EgressBlockedError(verdict)
        return TwinRouteSpec(
            method=spec.method.upper(),
            path_pattern=safe_path,
            response_template=safe_tpl,
            mutates_collection=safe_col,
            status_code=int(spec.status_code),
            redirect_to=safe_redirect,
        )

    for page_spec in pages:
        safe_url_path = _sanitize_url_path(page_spec.url_path, vocab)
        parser = _ZeroPhiHTMLSanitizer(projector, vocab)
        parser.feed(page_spec.sanitized_html)
        parser.close()
        clean_html = parser.get_sanitized_html()
        verdict = guard.check_and_record(clean_html, destination=f"stateful_twin_page:{safe_url_path}")
        if not verdict.allowed:
            raise EgressBlockedError(verdict)
        clean_page_routes = tuple(_sanitize_route(r) for r in page_spec.route_specs)
        all_routes.extend(clean_page_routes)
        sanitized_pages[safe_url_path] = TwinPageSpec(
            url_path=safe_url_path,
            sanitized_html=clean_html,
            route_specs=clean_page_routes,
        )

    for route_spec in api_routes:
        all_routes.append(_sanitize_route(route_spec))

    return StatefulTwinApp(
        pages=sanitized_pages,
        route_specs=all_routes,
        egress_guard=guard,
        base_url=base_url,
        canary_counts=projector.counts,
    )


def _call_readback(readback_check: Callable[..., bool], page: Any, twin_app: StatefulTwinApp) -> bool:
    try:
        params = [
            p for p in inspect.signature(readback_check).parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
    except (TypeError, ValueError):
        return bool(readback_check(page))
    if len(params) >= 2:
        return bool(readback_check(page, twin_app))
    return bool(readback_check(page))


def self_heal_multipage_in_twin(
    twin_app: StatefulTwinApp,
    failing_steps: Sequence[str],
    repair_fn: Callable[..., Any],
    *,
    readback_check: Callable[..., bool],
    max_turns: int = 3,
    start_path: Optional[str] = None,
    var_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run a multi-turn offline Playwright TDD loop against a StatefulTwinApp with 0 live network calls."""
    from playwright.sync_api import sync_playwright

    private_vars = dict(var_map or {})
    var_keys = sorted(private_vars.keys())
    safe_failing = [sanitize_ui_label_allowlist(s) or "step" for s in failing_steps]
    entry_path = start_path or (next(iter(twin_app.pages.keys())) if twin_app.pages else "/")
    limit = max(1, int(max_turns))
    last_entry_sha = ""
    last_failure_code = "none"
    accumulated_steps: List[DistilledRepairStep] = []
    turn_history: List[Dict[str, Any]] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            twin_app.install_on_page(page)

            for turn_idx in range(1, limit + 1):
                twin_app.reset_state()
                page.goto(twin_app.base_url + entry_path, wait_until="domcontentloaded")
                controls = observe_schema(page)
                outbound_payload: Dict[str, Any] = {
                    "controls": export_zero_phi_dom_twin(controls),
                    "var_keys": var_keys,
                    "structural_error": " ".join(safe_failing) or "error",
                    "feedback": {
                        "turn": turn_idx,
                        "max_turns": limit,
                        "last_failure_code": last_failure_code,
                    },
                }
                verdict = twin_app.egress_guard.check_and_record(
                    outbound_payload,
                    destination="multipage_twin_repair",
                )
                last_entry_sha = verdict.entry_sha256
                if not verdict.allowed:
                    turn_history.append({"turn": turn_idx, "outcome": f"egress_blocked:{verdict.reason}"})
                    break

                repair_out = repair_fn(outbound_payload, turn_idx)
                turn_steps: List[DistilledRepairStep] = []

                if isinstance(repair_out, str):
                    ast_res = verify_symbolic_repair_ast(repair_out, frozenset(var_keys))
                    if not ast_res.ok:
                        last_failure_code = "ast_firewall_rejected"
                        turn_history.append({"turn": turn_idx, "outcome": last_failure_code, "violations": ast_res.violations})
                        continue
                    ns: Dict[str, Any] = {"__builtins__": {"True": True, "False": False, "None": None}}
                    exec(compile(repair_out, "<multipage_twin_repair>", "exec"), ns)  # noqa: S102
                    fn = ns.get("repair")
                    if not callable(fn):
                        last_failure_code = "missing_repair_entrypoint"
                        turn_history.append({"turn": turn_idx, "outcome": last_failure_code})
                        continue
                    proxy = _ZeroPhiUIProxy(page, private_vars, controls)
                    try:
                        fn(proxy, {k: _SymVal(k, v) for k, v in private_vars.items()})
                    except Exception:
                        last_failure_code = "runtime_exception"
                        turn_history.append({"turn": turn_idx, "outcome": last_failure_code})
                        continue
                    turn_steps = list(proxy.recorded_steps)
                elif callable(repair_out):
                    res = repair_out(page, twin_app, private_vars)
                    if isinstance(res, list):
                        turn_steps = [s for s in res if isinstance(s, DistilledRepairStep)]
                else:
                    last_failure_code = "invalid_repair_output"
                    turn_history.append({"turn": turn_idx, "outcome": last_failure_code})
                    continue

                if _call_readback(readback_check, page, twin_app):
                    accumulated_steps = turn_steps
                    turn_history.append({"turn": turn_idx, "outcome": "verified"})
                    return {
                        "verified": True,
                        "turns_used": turn_idx,
                        "distilled_steps": accumulated_steps,
                        "live_network_requests_attempted": twin_app.live_network_requests_attempted,
                        "handled_twin_requests": list(twin_app.handled_twin_requests),
                        "store_snapshot": {k: list(v) for k, v in twin_app.store.items()},
                        "egress_audit": twin_app.egress_audit(),
                        "egress_ledger_entry_sha256": last_entry_sha,
                        "turn_history": turn_history,
                    }

                last_failure_code = "readback_refuted"
                turn_history.append({"turn": turn_idx, "outcome": last_failure_code})
        finally:
            browser.close()

    return {
        "verified": False,
        "turns_used": len(turn_history),
        "distilled_steps": accumulated_steps,
        "live_network_requests_attempted": twin_app.live_network_requests_attempted,
        "handled_twin_requests": list(twin_app.handled_twin_requests),
        "store_snapshot": {k: list(v) for k, v in twin_app.store.items()},
        "egress_audit": twin_app.egress_audit(),
        "egress_ledger_entry_sha256": last_entry_sha,
        "turn_history": turn_history,
    }
