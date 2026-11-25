"""Zero-PHI Digital Twin Snapshotter and Offline TDD Self-Healing Compiler."""

from __future__ import annotations

import html
import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

from openadapt_kernel.egress_guard import (
    EgressBlockedError,
    EgressPolicy,
    EgressVerdict,
    LocalEgressGuard,
    inspect_outbound_payload,
)
from openadapt_kernel.macro import observe_schema
from openadapt_kernel.zero_phi_repair import (
    DistilledRepairStep,
    _has_disabled_submit,
    _SymVal,
    _ZeroPhiUIProxy,
    export_zero_phi_dom_twin,
    sanitize_ui_label_allowlist,
    verify_symbolic_repair_ast,
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

_DATE_SPAN_RE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b")
_NUM_OR_ID_SPAN_RE = re.compile(
    r"(?:\$\s?\d[\d,]*(?:\.\d{2})?|\b\d{3}-\d{2}-\d{4}\b|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b|\b[A-Za-z]{1,6}-\d{2,}[A-Za-z0-9-]*\b|\b\d[\d,./-]*\b)"
)
_WORD_OR_CANARY_RE = re.compile(r"CANARY_[A-Z]+_\d+|[A-Za-z0-9_]+")

_EXTRACT_DOM_TREE_JS = """() => {
  const SKIP = new Set(['SCRIPT','STYLE','IMG','CANVAS','SVG','VIDEO','AUDIO','IFRAME','OBJECT','EMBED','PICTURE','SOURCE','NOSCRIPT','LINK','META']);
  const root = document.querySelector('[role="dialog"], dialog[open]') || document.body;
  if (!root) return null;
  const safeStyle = (el) => {
    const cs = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const parts = [];
    if (cs.display && cs.display !== 'inline') parts.push('display:' + cs.display);
    if (r.width >= 1 && r.width <= 4000) parts.push('width:' + Math.round(r.width) + 'px');
    if (r.height >= 1 && r.height <= 4000) parts.push('height:' + Math.round(r.height) + 'px');
    if (cs.visibility === 'hidden') parts.push('visibility:hidden');
    if (cs.opacity && cs.opacity !== '1') parts.push('opacity:' + cs.opacity);
    return parts.join(';');
  };
  const walk = (node, depth) => {
    if (depth > 20 || !node) return null;
    if (node.nodeType === 3) {
      const txt = (node.nodeValue || '').replace(/\\s+/g, ' ').trim();
      return txt ? { kind: 'text', text: txt } : null;
    }
    if (node.nodeType !== 1) return null;
    const tag = node.tagName.toUpperCase();
    if (SKIP.has(tag)) return null;
    const low = tag.toLowerCase();
    const attrs = {};
    for (const k of ['type','role','data-oa-idx','placeholder','aria-label','aria-required','aria-disabled','aria-checked','aria-expanded','aria-selected','tabindex']) {
      const v = node.getAttribute(k);
      if (v !== null && v !== '') attrs[k] = v;
    }
    if (node.disabled || node.hasAttribute('disabled')) attrs.disabled = 'true';
    if (node.required || node.hasAttribute('required')) attrs.required = 'true';
    if (low === 'input' && (node.type === 'checkbox' || node.type === 'radio') && node.checked) {
      attrs.checked = 'true';
    }
    if (low === 'option') {
      attrs.opt_value = node.getAttribute('value') || '';
      if (node.selected) attrs.selected = 'true';
    }
    const st = safeStyle(node);
    if (st) attrs.style = st;
    const children = [];
    if (low !== 'textarea') {
      for (const ch of node.childNodes) {
        const res = walk(ch, depth + 1);
        if (res) children.push(res);
      }
    }
    return { kind: 'el', tag: low, attrs, children };
  };
  return walk(root, 0);
}"""

_TWIN_FORM_HARNESS_JS = """() => {
  const containers = Array.from(document.querySelectorAll('form, [role="dialog"]'));
  if (containers.length === 0 && document.body) containers.push(document.body);
  const isSubmitBtn = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const txt = (el.innerText || el.value || '').trim().toLowerCase();
    return (tag === 'button' || (tag === 'input' && type === 'submit')) &&
      (type === 'submit' || ['save', 'submit', 'create', 'confirm', 'apply'].includes(txt));
  };
  for (const box of containers) {
    const submits = Array.from(box.querySelectorAll('button, input[type="submit"]')).filter(isSubmitBtn);
    for (const btn of submits) {
      if (btn.disabled) btn.setAttribute('data-oa-initially-disabled', 'true');
      btn.addEventListener('click', () => {
        if (!btn.disabled && btn.getAttribute('aria-disabled') !== 'true') {
          document.body.setAttribute('data-oa-twin-submitted', 'true');
        }
      });
    }
    const sync = () => {
      const reqs = Array.from(box.querySelectorAll('[required], [aria-required="true"]'));
      const ok = reqs.every((el) => {
        const tag = el.tagName.toLowerCase();
        const t = (el.getAttribute('type') || '').toLowerCase();
        if (t === 'checkbox' || t === 'radio') return Boolean(el.checked);
        if (tag === 'select') return String(el.value || '').trim().length > 0;
        return String(el.value || '').trim().length > 0;
      });
      for (const btn of submits) {
        if (btn.getAttribute('data-oa-initially-disabled') === 'true') {
          btn.disabled = !ok;
          btn.setAttribute('aria-disabled', ok ? 'false' : 'true');
        }
      }
    };
    box.addEventListener('input', sync, true);
    box.addEventListener('change', sync, true);
    sync();
  }
}"""

_TWIN_POSTCONDITION_JS = """() => {
  const reqs = Array.from(document.querySelectorAll('[required], [aria-required="true"]'));
  const allReqOk = reqs.every((el) => {
    const tag = el.tagName.toLowerCase();
    const t = (el.getAttribute('type') || '').toLowerCase();
    if (t === 'checkbox' || t === 'radio') return Boolean(el.checked);
    if (tag === 'select') return String(el.value || '').trim().length > 0;
    return String(el.value || '').trim().length > 0;
  });
  const submitted = document.body && document.body.getAttribute('data-oa-twin-submitted') === 'true';
  const submitBtns = Array.from(document.querySelectorAll('button, input[type="submit"]')).filter((el) => {
    const type = (el.getAttribute('type') || '').toLowerCase();
    const txt = (el.innerText || el.value || '').trim().toLowerCase();
    return type === 'submit' || ['save', 'submit', 'create', 'confirm'].includes(txt);
  });
  const anyEnabled = submitBtns.length === 0 || submitBtns.some((b) => !b.disabled);
  return { allReqOk, submitted, anyEnabled };
}"""


class _CanaryProjector:
    """Deterministic type-preserving allowlist projector emitting CANARY_* tokens."""

    def __init__(self, vocab: FrozenSet[str]) -> None:
        self._vocab = vocab
        self._seen: Dict[Tuple[str, str], str] = {}
        self.counts: Dict[str, int] = {"TEXT": 0, "DATE": 0, "NUM": 0}

    def _mint(self, kind: str, raw_key: str) -> str:
        key = (kind, raw_key)
        if key not in self._seen:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self._seen[key] = f"CANARY_{kind}_{self.counts[kind]}"
        return self._seen[key]

    def project(self, raw: str) -> str:
        if not raw or not raw.strip():
            return ""
        step1 = _DATE_SPAN_RE.sub(lambda m: f" {self._mint('DATE', m.group(0))} ", raw)
        step2 = _NUM_OR_ID_SPAN_RE.sub(lambda m: f" {self._mint('NUM', m.group(0))} ", step1)

        out: List[str] = []
        non_allowed_buf: List[str] = []

        def _flush_buf() -> None:
            if non_allowed_buf:
                out.append(self._mint("TEXT", " ".join(non_allowed_buf)))
                non_allowed_buf.clear()

        for tok in _WORD_OR_CANARY_RE.findall(step2):
            if tok.startswith("CANARY_"):
                _flush_buf()
                out.append(tok)
            elif tok.lower() in self._vocab:
                _flush_buf()
                out.append(tok)
            else:
                non_allowed_buf.append(tok)
        _flush_buf()
        return " ".join(out)


def _render_safe_node(node: Dict[str, Any], projector: _CanaryProjector) -> str:
    if node.get("kind") == "text":
        projected = projector.project(str(node.get("text", "")))
        return html.escape(projected, quote=False)

    raw_tag = str(node.get("tag", "div")).lower()
    tag = raw_tag if raw_tag in _SAFE_TAGS else "div"
    raw_attrs: Dict[str, Any] = dict(node.get("attrs") or {})
    rendered_attrs: List[str] = []

    if tag == "input":
        itype = str(raw_attrs.get("type", "text")).lower()
        rendered_attrs.append(f'type="{itype if itype in _SAFE_INPUT_TYPES else "text"}"')
    elif tag == "button":
        btype = str(raw_attrs.get("type", "button")).lower()
        if btype in ("button", "submit", "reset"):
            rendered_attrs.append(f'type="{btype}"')

    role = str(raw_attrs.get("role", "")).lower()
    if role in _SAFE_ROLES:
        rendered_attrs.append(f'role="{role}"')

    oa_idx = str(raw_attrs.get("data-oa-idx", ""))
    if oa_idx.isdigit() and len(oa_idx) <= 4:
        rendered_attrs.append(f'data-oa-idx="{oa_idx}"')

    tabindex = str(raw_attrs.get("tabindex", ""))
    if re.fullmatch(r"-?\d{1,2}", tabindex):
        rendered_attrs.append(f'tabindex="{tabindex}"')

    for bool_attr in ("disabled", "required", "checked", "selected"):
        if raw_attrs.get(bool_attr) == "true":
            rendered_attrs.append(bool_attr)

    for aria_bool in ("aria-required", "aria-disabled", "aria-checked", "aria-expanded", "aria-selected"):
        val = str(raw_attrs.get(aria_bool, "")).lower()
        if val in ("true", "false"):
            rendered_attrs.append(f'{aria_bool}="{val}"')

    for text_attr in ("placeholder", "aria-label"):
        if text_attr in raw_attrs:
            proj = projector.project(str(raw_attrs[text_attr]))
            if proj:
                rendered_attrs.append(f'{text_attr}="{html.escape(proj, quote=True)}"')

    style = str(raw_attrs.get("style", ""))
    if style and re.fullmatch(r"[A-Za-z0-9:;.% -]+", style):
        rendered_attrs.append(f'style="{style}"')

    children_html = "".join(
        _render_safe_node(ch, projector) for ch in (node.get("children") or [])
    )

    if tag == "option":
        raw_opt_val = str(raw_attrs.get("opt_value", ""))
        if raw_opt_val == "":
            rendered_attrs.append('value=""')
        else:
            proj_val = projector.project(raw_opt_val) or children_html.strip()
            rendered_attrs.append(f'value="{html.escape(proj_val, quote=True)}"')

    attr_str = (" " + " ".join(rendered_attrs)) if rendered_attrs else ""
    if tag in _VOID_TAGS:
        return f"<{tag}{attr_str}>"
    return f"<{tag}{attr_str}>{children_html}</{tag}>"


@dataclass(frozen=True)
class DigitalTwinSnapshot:
    """Zero-PHI runnable HTML+CSS digital twin verified by the egress gatekeeper."""

    sanitized_html: str
    controls: List[Dict[str, Any]]
    canary_counts: Dict[str, int]
    egress_verdict: EgressVerdict
    html_sha256: str


def capture_zero_phi_digital_twin(
    page: Any,
    *,
    policy: Optional[EgressPolicy] = None,
) -> DigitalTwinSnapshot:
    """Extract a runnable, zero-PHI HTML+CSS digital twin of the active page/modal."""
    pol = policy or EgressPolicy()
    controls = observe_schema(page)
    raw_tree = page.evaluate(_EXTRACT_DOM_TREE_JS)
    projector = _CanaryProjector(pol.effective_tokens())
    body_html = _render_safe_node(raw_tree, projector) if isinstance(raw_tree, dict) else "<div></div>"
    sanitized_html = (
        '<!doctype html><html><head><meta charset="utf-8"><title>Twin</title></head>'
        f"<body>{body_html}</body></html>"
    )
    verdict = inspect_outbound_payload(sanitized_html, policy=pol)
    if not verdict.allowed:
        raise EgressBlockedError(verdict)
    return DigitalTwinSnapshot(
        sanitized_html=sanitized_html,
        controls=export_zero_phi_dom_twin(controls),
        canary_counts=dict(projector.counts),
        egress_verdict=verdict,
        html_sha256=verdict.payload_sha256,
    )


@dataclass
class TwinHealResult:
    """Result of offline TDD self-healing inside a Zero-PHI Digital Twin."""

    verified: bool
    synthesized_code: str = ""
    distilled_steps: List[DistilledRepairStep] = field(default_factory=list)
    egress_ledger_entry_sha256: str = ""
    egress_payload_sha256: str = ""
    turns_used: int = 0
    live_network_requests_attempted: int = 0
    ast_violations: List[str] = field(default_factory=list)
    failure_reason: str = ""
    turn_history: List[Dict[str, Any]] = field(default_factory=list)


def _invoke_coding_agent(
    fn: Callable[..., str],
    outbound_payload: Dict[str, Any],
    twin_html: str,
    var_keys: List[str],
    safe_error: str,
    feedback: Dict[str, Any],
) -> str:
    try:
        params = [
            p for p in inspect.signature(fn).parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return fn(outbound_payload)
    if has_varargs or len(params) >= 4:
        return fn(twin_html, var_keys, safe_error, feedback)
    if len(params) == 3:
        return fn(twin_html, var_keys, safe_error)
    if len(params) == 2:
        return fn(twin_html, var_keys)
    return fn(outbound_payload)


def self_heal_in_digital_twin(
    twin: DigitalTwinSnapshot,
    var_map: Dict[str, str],
    coding_agent_fn: Callable[..., str],
    *,
    egress_guard: LocalEgressGuard,
    max_turns: int = 3,
    structural_error: str = "submit disabled",
    postcondition_fn: Optional[Callable[[Any], bool]] = None,
) -> TwinHealResult:
    """Run bounded offline TDD self-healing inside twin.sanitized_html with mandatory egress gating."""
    from playwright.sync_api import sync_playwright

    var_keys = sorted(var_map.keys())
    safe_error = sanitize_ui_label_allowlist(structural_error) or "error"
    limit = max(1, int(max_turns))
    attempted_net_reqs = 0
    last_entry_sha = ""
    last_payload_sha = ""
    last_failure_code = ""
    last_ast_violations: List[str] = []
    history: List[Dict[str, Any]] = []
    executed_codes: List[str] = []
    accumulated_steps: List[DistilledRepairStep] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()

            def _abort_all_network(route: Any) -> None:
                nonlocal attempted_net_reqs
                attempted_net_reqs += 1
                route.abort("blockedbyclient")

            page.route("**/*", _abort_all_network)
            page.set_content(twin.sanitized_html, wait_until="domcontentloaded")
            page.evaluate(_TWIN_FORM_HARNESS_JS)

            for turn_idx in range(1, limit + 1):
                controls = observe_schema(page)
                ctrl_twin = export_zero_phi_dom_twin(controls)
                submit_disabled = _has_disabled_submit(controls)
                feedback: Dict[str, Any] = {
                    "turn": turn_idx,
                    "max_turns": limit,
                    "submit_still_disabled": submit_disabled,
                    "last_failure_code": last_failure_code or "none",
                }
                outbound_payload: Dict[str, Any] = {
                    "sanitized_html": twin.sanitized_html,
                    "controls": ctrl_twin,
                    "var_keys": var_keys,
                    "structural_error": safe_error,
                    "feedback": feedback,
                }
                verdict = egress_guard.check_and_record(
                    outbound_payload,
                    destination="coding_agent",
                )
                last_entry_sha = verdict.entry_sha256
                last_payload_sha = verdict.payload_sha256
                if not verdict.allowed:
                    history.append({"turn": turn_idx, "outcome": f"egress_blocked:{verdict.reason}"})
                    return TwinHealResult(
                        verified=False,
                        egress_ledger_entry_sha256=last_entry_sha,
                        egress_payload_sha256=last_payload_sha,
                        turns_used=turn_idx,
                        live_network_requests_attempted=attempted_net_reqs,
                        failure_reason=f"egress_blocked:{verdict.reason}",
                        turn_history=history,
                    )

                code_str = _invoke_coding_agent(
                    coding_agent_fn, outbound_payload, twin.sanitized_html, var_keys, safe_error, feedback
                )
                ast_check = verify_symbolic_repair_ast(code_str, frozenset(var_keys))
                if not ast_check.ok:
                    last_failure_code = "ast_firewall_rejected"
                    last_ast_violations = list(ast_check.violations)
                    history.append({"turn": turn_idx, "outcome": last_failure_code, "code": code_str})
                    continue

                ns: Dict[str, Any] = {"__builtins__": {"True": True, "False": False, "None": None}}
                exec(compile(code_str, "<twin_symbolic_repair>", "exec"), ns)  # noqa: S102
                repair_fn = ns.get("repair")
                if not callable(repair_fn):
                    return TwinHealResult(
                        verified=False,
                        synthesized_code=code_str,
                        egress_ledger_entry_sha256=last_entry_sha,
                        egress_payload_sha256=last_payload_sha,
                        turns_used=turn_idx,
                        live_network_requests_attempted=attempted_net_reqs,
                        failure_reason="missing_repair_entrypoint",
                        turn_history=history,
                    )

                proxy = _ZeroPhiUIProxy(page, var_map, controls)
                executed_codes.append(code_str)
                try:
                    repair_fn(proxy, {k: _SymVal(k, v) for k, v in var_map.items()})
                except Exception as exc:
                    last_failure_code = "submit_disabled" if isinstance(exc, PermissionError) else "runtime_exception"
                    accumulated_steps.extend(proxy.recorded_steps)
                    history.append({"turn": turn_idx, "outcome": last_failure_code, "code": code_str})
                    continue

                accumulated_steps.extend(proxy.recorded_steps)
                if postcondition_fn is not None:
                    ok = bool(postcondition_fn(page))
                else:
                    state = page.evaluate(_TWIN_POSTCONDITION_JS)
                    ok = bool(state.get("allReqOk") and (state.get("submitted") or state.get("anyEnabled")))
                if ok:
                    history.append({"turn": turn_idx, "outcome": "verified", "code": code_str})
                    return TwinHealResult(
                        verified=True,
                        synthesized_code="\n\n# --- turn ---\n".join(executed_codes),
                        distilled_steps=accumulated_steps,
                        egress_ledger_entry_sha256=last_entry_sha,
                        egress_payload_sha256=last_payload_sha,
                        turns_used=turn_idx,
                        live_network_requests_attempted=attempted_net_reqs,
                        turn_history=history,
                    )
                last_failure_code = "readback_refuted"
                history.append({"turn": turn_idx, "outcome": last_failure_code, "code": code_str})
        finally:
            browser.close()

    return TwinHealResult(
        verified=False,
        synthesized_code="\n\n# --- turn ---\n".join(executed_codes),
        distilled_steps=accumulated_steps,
        egress_ledger_entry_sha256=last_entry_sha,
        egress_payload_sha256=last_payload_sha,
        turns_used=len(history),
        live_network_requests_attempted=attempted_net_reqs,
        ast_violations=last_ast_violations,
        failure_reason="twin_postcondition_refuted",
        turn_history=history,
    )
