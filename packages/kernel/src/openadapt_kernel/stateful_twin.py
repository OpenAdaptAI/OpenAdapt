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
    {"dialog", "button", "textbox", "combobox", "checkbox", "radio", "listbox", "option", "menu", "menuitem", "form", "main", "region", "alert", "tab", "table"}
)
_SAFE_FORM_METHODS: FrozenSet[str] = frozenset({"get", "post"})
_SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9_/-]*$")
_VAR_PLACEHOLDER_RE = re.compile(r"^\$var_\d+$")
_SAFE_PLATFORMS: FrozenSet[str] = frozenset({"uia", "macos_ax", "at_spi", "citrix_box"})
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
                raw_attr = attr_map[text_attr].strip()
                if text_attr == "placeholder" and _VAR_PLACEHOLDER_RE.fullmatch(raw_attr):
                    proj = raw_attr
                else:
                    proj = self._projector.project(raw_attr)
                if proj:
                    rendered.append(f'{text_attr}="{html.escape(proj, quote=True)}"')

        style = attr_map.get("style", "").strip()
        if style and re.fullmatch(r"[A-Za-z0-9:;.% -]+", style):
            rendered.append(f'style="{style}"')

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
        chunks = re.split(r"(\$var_\d+)", data)
        out_parts: List[str] = []
        for chunk in chunks:
            if not chunk or not chunk.strip():
                continue
            if _VAR_PLACEHOLDER_RE.fullmatch(chunk):
                out_parts.append(chunk)
            else:
                proj = self._projector.project(chunk)
                if proj:
                    out_parts.append(proj)
        if out_parts:
            self._out.append(html.escape(" ".join(out_parts), quote=False))

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


@dataclass(frozen=True)
class A11yNodeSpec:
    """Platform-neutral desktop or Citrix accessibility node for Zero-PHI twin projection."""

    node_id: str
    role: str
    name: str
    value: str = ""
    bbox: Tuple[int, int, int, int] = (0, 0, 0, 0)
    enabled: bool = True
    checked: Optional[bool] = None
    expanded: Optional[bool] = None
    platform: str = "uia"
    options: Tuple[str, ...] = ()


@dataclass(frozen=True)
class A11yScreenSpec:
    """Desktop or Citrix screen specification with accessibility nodes and state transitions."""

    screen_id: str
    url_path: str
    nodes: Tuple[A11yNodeSpec, ...] = ()
    transitions: Tuple[TwinRouteSpec, ...] = ()
    store_collection: str = ""


def _normalize_a11y_role(raw_role: str) -> str:
    low = re.sub(r"[^a-z0-9]+", "", (raw_role or "").lower())
    for prefix in ("controltype", "ax", "role"):
        if low.startswith(prefix) and len(low) > len(prefix):
            low = low[len(prefix):]
    if low in ("button", "pushbutton", "submit", "submitbutton"):
        return "button"
    if low in ("edit", "textbox", "textfield", "textarea", "text", "entry", "input"):
        return "edit"
    if low in ("combobox", "popupbutton", "dropdown", "select", "listbox"):
        return "combobox"
    if low in ("checkbox", "check", "togglebutton"):
        return "checkbox"
    if low in ("datagrid", "table", "grid"):
        return "table"
    if low in ("tabitem", "tab", "pagetab"):
        return "tab"
    return "static"


def _bbox_style(bbox: Tuple[int, int, int, int]) -> str:
    x, y, w, h = bbox
    parts = ["position:relative"]
    if w > 0:
        parts.append(f"width:{min(max(int(w), 1), 4000)}px")
    if h > 0:
        parts.append(f"height:{min(max(int(h), 1), 4000)}px")
    if x > 0:
        parts.append(f"left:{min(int(x), 4000)}px")
    if y > 0:
        parts.append(f"top:{min(int(y), 4000)}px")
    return ";".join(parts)


def project_a11y_tree_to_zero_phi_twin(
    screens: Sequence[A11yScreenSpec],
    *,
    egress_policy: Optional[EgressPolicy] = None,
    ledger_path: Optional[Union[str, Path]] = None,
    base_url: str = "http://twin.local",
) -> StatefulTwinApp:
    """Project native desktop (UIA/macOS AX/AT-SPI) or Citrix screen trees into a Zero-PHI StatefulTwinApp."""
    base_policy = _default_twin_policy(egress_policy)
    vocab = base_policy.effective_tokens()
    projector = _CanaryProjector(vocab)
    var_keys_used: List[str] = []
    var_counter = 0

    def _next_var_placeholder() -> str:
        nonlocal var_counter
        var_counter += 1
        vk = f"$var_{var_counter}"
        var_keys_used.append(vk)
        return vk

    def _safe_field_name(raw: str, fallback: str) -> str:
        for tok in re.findall(r"[A-Za-z0-9_]+", raw or ""):
            if tok.lower() in vocab:
                return tok.lower()
        return fallback

    page_specs: List[TwinPageSpec] = []
    all_routes: List[TwinRouteSpec] = []

    for screen in screens:
        post_route = next((r for r in screen.transitions if r.method.upper() == "POST"), None)
        form_action = _sanitize_url_path(post_route.path_pattern, vocab) if post_route else ""
        parts: List[str] = []
        if form_action:
            parts.append(f'<form method="post" action="{form_action}">')

        for idx, node in enumerate(screen.nodes):
            if node.platform not in _SAFE_PLATFORMS:
                raise ValueError(f"Unsupported desktop platform: {node.platform!r}")
            norm_role = _normalize_a11y_role(node.role)
            safe_name = projector.project(node.name) or norm_role
            style_attr = f' style="{_bbox_style(node.bbox)}"' if any(v > 0 for v in node.bbox) else ""
            dis_attr = " disabled" if not node.enabled else ""
            exp_attr = f' aria-expanded="{"true" if node.expanded else "false"}"' if node.expanded is not None else ""
            idx_attr = f' data-oa-idx="{idx}"'

            if norm_role == "button":
                is_submit = bool(form_action) and any(
                    w in (node.name or "").lower()
                    for w in ("save", "submit", "create", "confirm", "apply", "continue", "add", "ok")
                )
                btype = "submit" if is_submit else "button"
                parts.append(
                    f'<button type="{btype}"{idx_attr}{dis_attr}{exp_attr}{style_attr}>'
                    f"{html.escape(safe_name, quote=False)}</button>"
                )
            elif norm_role == "edit":
                fname = _safe_field_name(node.name, "field")
                ph = _next_var_placeholder() if node.value else safe_name
                parts.append(
                    f"<label>{html.escape(safe_name, quote=False)}</label>"
                    f'<input type="text" name="{fname}" aria-label="{html.escape(safe_name, quote=True)}" '
                    f'placeholder="{html.escape(ph, quote=True)}"{idx_attr}{dis_attr}{style_attr}>'
                )
            elif norm_role == "combobox":
                fname = _safe_field_name(node.name, "option")
                opts = list(node.options) if node.options else ["Select", "Primary"]
                opt_html = []
                for oi, opt in enumerate(opts):
                    safe_opt = projector.project(opt) or "option"
                    val_str = "" if (oi == 0 and opt.strip().lower() in ("", "select")) else safe_opt
                    opt_html.append(
                        f'<option value="{html.escape(val_str, quote=True)}">{html.escape(safe_opt, quote=False)}</option>'
                    )
                parts.append(
                    f"<label>{html.escape(safe_name, quote=False)}</label>"
                    f'<select name="{fname}" aria-label="{html.escape(safe_name, quote=True)}"'
                    f"{idx_attr}{dis_attr}{exp_attr}{style_attr}>{''.join(opt_html)}</select>"
                )
            elif norm_role == "checkbox":
                fname = _safe_field_name(node.name, "check")
                chk = " checked" if node.checked is True else ""
                parts.append(
                    f"<label>{html.escape(safe_name, quote=False)}</label>"
                    f'<input type="checkbox" name="{fname}" aria-label="{html.escape(safe_name, quote=True)}"'
                    f"{idx_attr}{dis_attr}{chk}{style_attr}>"
                )
            elif norm_role == "table":
                cell_val = _next_var_placeholder() if node.value else "item"
                parts.append(
                    f'<table role="table" aria-label="{html.escape(safe_name, quote=True)}"{style_attr}>'
                    f"<tbody><tr><td>{html.escape(cell_val, quote=False)}</td></tr></tbody></table>"
                )
            elif norm_role == "tab":
                parts.append(
                    f'<button type="button" role="tab"{idx_attr}{dis_attr}{exp_attr}{style_attr}>'
                    f"{html.escape(safe_name, quote=False)}</button>"
                )
            else:
                display_txt = safe_name
                if node.value:
                    display_txt = f"{safe_name} {_next_var_placeholder()}"
                parts.append(f"<div{style_attr}>{html.escape(display_txt, quote=False)}</div>")

        if form_action:
            parts.append("</form>")

        if screen.store_collection and screen.store_collection.lower() in vocab:
            parts.append(f'<div data-oa-twin-store="{screen.store_collection.lower()}"></div>')

        page_specs.append(
            TwinPageSpec(
                url_path=screen.url_path,
                sanitized_html=f"<div>{''.join(parts)}</div>",
                route_specs=screen.transitions,
            )
        )

    final_policy = EgressPolicy(
        allowed_tokens=base_policy.allowed_tokens,
        include_html_aria_vocab=base_policy.include_html_aria_vocab,
        include_clinical_vocab=base_policy.include_clinical_vocab,
        extra_allowed_tokens=base_policy.extra_allowed_tokens,
        allowed_var_keys=base_policy.allowed_var_keys | frozenset(var_keys_used),
    )
    return synthesize_zero_phi_stateful_twin(
        page_specs,
        api_routes=all_routes,
        egress_policy=final_policy,
        ledger_path=ledger_path,
        base_url=base_url,
    )
