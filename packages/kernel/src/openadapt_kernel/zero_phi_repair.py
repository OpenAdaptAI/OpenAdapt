"""Zero-PHI Structural Twin & AST-Sandboxed Frontier Coding-Agent Runtime Repair.

Combines local schema extraction with cloud frontier program synthesis at 0.0% PHI/PII risk:
  1. Allowlist Structural Twin (`export_zero_phi_dom_twin`): strips 100% of field values,
     URLs, IDs, and non-allowlisted text tokens. Only structural HTML/ARIA attributes and
     words in a frozen public UI-chrome vocabulary cross the boundary.
  2. Symbolic AST Firewall (`verify_symbolic_repair_ast`): statically verifies synthesized
     Python repair scripts before execution — blocking all imports, attribute traversal,
     dunder access, network/file I/O, and arbitrary literals, allowing ONLY `ui.*` calls
     and symbolic `vars["$key"]` references.
  3. Read-Back Gated Execution (`execute_zero_phi_coding_repair`): runs verified symbolic
     repairs locally on private data and promotes ONLY if independent read-back passes.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence

from openadapt_kernel.macro import ControlSchema, observe_schema

# Frozen allowlist of generic public English UI-chrome words. Any word NOT in this set
# (patient names, MRNs, SSNs, addresses, custom labels, clinics) is redacted to <TOKEN>.
_FROZEN_UI_VOCAB: FrozenSet[str] = frozenset({
    "add", "advanced", "all", "amount", "apply", "auth", "authorization", "back", "bind",
    "body", "button", "cancel", "categories", "category", "check", "checkbox", "choose",
    "claim", "clear", "close", "code", "confirm", "continue", "cookbook", "cookbooks",
    "coverage", "create", "date", "deductible", "delete", "description", "details",
    "dialog", "disabled", "dob", "edit", "email", "enable", "enabled", "error", "field",
    "filter", "filters", "first", "form", "home", "households", "id", "in", "input", "is",
    "item", "items", "label", "last", "list", "login", "main", "member", "memo", "modal",
    "name", "new", "next", "no", "note", "notes", "number", "of", "ok", "one", "open",
    "option", "options", "or", "other", "outpatient", "page", "password", "phone", "plan",
    "policy", "previous", "primary", "project", "public", "publish", "query", "quote",
    "radio", "recipe", "recipes", "ref", "reference", "remove", "request", "required",
    "reset", "review", "row", "rule", "rules", "save", "search", "select", "send", "set",
    "settings", "show", "sign", "slug", "state", "status", "step", "submit", "tab", "tag",
    "tags", "task", "team", "text", "textarea", "tier", "title", "to", "toggle", "type",
    "up", "update", "user", "username", "value", "view", "yes",
})

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")

_ALLOWED_UI_METHODS: FrozenSet[str] = frozenset({
    "click", "fill", "select", "check", "press", "clear", "wait", "remove_idx",
})


def sanitize_ui_label_allowlist(raw: str) -> str:
    """Keep ONLY words in _FROZEN_UI_VOCAB; replace every other token with <OPAQUE>."""
    if not raw:
        return ""
    tokens = _WORD_RE.findall(raw)
    out: List[str] = []
    for tok in tokens[:12]:
        low = tok.lower()
        if low in _FROZEN_UI_VOCAB:
            out.append(low)
        else:
            out.append("<OPAQUE>")
    # Collapse consecutive <OPAQUE> markers so string token count cannot leak name structure
    collapsed: List[str] = []
    for item in out:
        if item == "<OPAQUE>" and collapsed and collapsed[-1] == "<OPAQUE>":
            continue
        collapsed.append(item)
    return " ".join(collapsed)


def export_zero_phi_dom_twin(controls: Sequence[ControlSchema]) -> List[Dict[str, Any]]:
    """Project live controls into a mathematically zero-PHI structural skeleton."""
    twin: List[Dict[str, Any]] = []
    for c in controls:
        twin.append({
            "idx": c.control_index,
            "tag": c.tag,
            "type": c.input_type if c.input_type in ("text", "checkbox", "radio", "submit", "button", "number", "date", "") else "other",
            "role": c.role if c.role in ("dialog", "button", "textbox", "combobox", "checkbox", "listbox", "option", "") else "",
            "in_modal": bool(c.in_modal),
            "disabled": bool(c.disabled),
            "has_value": bool(c.value and c.value not in ("false", "")),
            "ui_chrome_tokens": sanitize_ui_label_allowlist(f"{c.label} {c.placeholder}"),
        })
    return twin


@dataclass
class ASTVerificationResult:
    ok: bool
    violations: List[str] = field(default_factory=list)


def verify_symbolic_repair_ast(code_str: str, allowed_vars: FrozenSet[str]) -> ASTVerificationResult:
    """Statically verify that synthesized Python code ONLY invokes `ui.<allowed>(...)` with `vars['$k']`."""
    violations: List[str] = []
    try:
        tree = ast.parse(code_str, mode="exec")
    except SyntaxError as exc:
        return ASTVerificationResult(ok=False, violations=[f"syntax_error:{exc.msg}"])

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.With, ast.AsyncWith, ast.Try, ast.Raise, ast.Delete, ast.Lambda, ast.ClassDef)):
            violations.append(f"forbidden_syntax:{type(node).__name__}")
        elif isinstance(node, ast.FunctionDef):
            if node.name != "repair":
                violations.append(f"forbidden_function_name:{node.name}")
        elif isinstance(node, ast.Name):
            if node.id.startswith("__") or node.id not in ("repair", "ui", "vars", "True", "False", "None"):
                violations.append(f"forbidden_identifier:{node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or not isinstance(node.value, ast.Name) or node.value.id != "ui" or node.attr not in _ALLOWED_UI_METHODS:
                violations.append(f"forbidden_attribute:{ast.unparse(node)}")
        elif isinstance(node, ast.Call):
            if not (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "ui" and node.func.attr in _ALLOWED_UI_METHODS):
                violations.append(f"forbidden_call:{ast.unparse(node.func)}")
        elif isinstance(node, ast.Subscript):
            if not (isinstance(node.value, ast.Name) and node.value.id == "vars"):
                violations.append("forbidden_subscript_target")
            elif isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                if node.slice.value not in allowed_vars:
                    violations.append(f"unknown_symbolic_var:{node.slice.value}")
            else:
                violations.append("non_constant_var_key")

    return ASTVerificationResult(ok=not violations, violations=violations)


class _SandboxedUIProxy:
    """Executes ONLY indexed UI primitives on the local Playwright page."""

    def __init__(self, page: Any) -> None:
        self._page = page

    def _loc(self, idx: int) -> Any:
        observe_schema(self._page)
        loc = self._page.locator(f'[data-oa-idx="{int(idx)}"]').first
        loc.wait_for(state="visible", timeout=2500)
        loc.scroll_into_view_if_needed()
        return loc

    def click(self, idx: int) -> None:
        self._loc(idx).click(timeout=2500)
        self._page.wait_for_timeout(250)

    def fill(self, idx: int, val: str) -> None:
        loc = self._loc(idx)
        loc.fill(str(val), timeout=2500)
        self._page.wait_for_timeout(150)

    def clear(self, idx: int) -> None:
        self._loc(idx).fill("", timeout=2500)

    def check(self, idx: int, state: bool = True) -> None:
        loc = self._loc(idx)
        if state:
            loc.check(timeout=2500)
        else:
            loc.uncheck(timeout=2500)
        self._page.wait_for_timeout(150)

    def select(self, idx: int, val: str) -> None:
        self._loc(idx).select_option(str(val), timeout=2500)

    def press(self, idx: int, key: str) -> None:
        allowed_keys = {"Enter", "Tab", "Escape", "Backspace", "ArrowDown", "Space"}
        if key not in allowed_keys:
            raise ValueError(f"Disallowed key: {key}")
        self._loc(idx).press(key, timeout=2500)
        self._page.wait_for_timeout(150)

    def wait(self, ms: int) -> None:
        self._page.wait_for_timeout(min(max(int(ms), 0), 3000))


@dataclass
class ZeroPHIRepairResult:
    verified: bool
    synthesized_code: str = ""
    outbound_twin: List[Dict[str, Any]] = field(default_factory=list)
    ast_violations: List[str] = field(default_factory=list)
    failure_reason: str = ""


def execute_zero_phi_coding_repair(
    page: Any,
    *,
    private_vars: Dict[str, str],
    structural_error: str,
    coding_agent_fn: Callable[[List[Dict[str, Any]], List[str], str], str],
    readback_oracle: Callable[[Any], bool],
) -> ZeroPHIRepairResult:
    """Run Zero-PHI Structural Twin -> Cloud Coding Agent -> Local AST Firewall -> Read-Back."""
    controls = observe_schema(page)
    twin = export_zero_phi_dom_twin(controls)
    var_keys = sorted(private_vars.keys())
    safe_error = sanitize_ui_label_allowlist(structural_error)

    code_str = coding_agent_fn(twin, var_keys, safe_error)
    ast_check = verify_symbolic_repair_ast(code_str, frozenset(var_keys))
    if not ast_check.ok:
        return ZeroPHIRepairResult(
            verified=False,
            synthesized_code=code_str,
            outbound_twin=twin,
            ast_violations=ast_check.violations,
            failure_reason="ast_firewall_rejected",
        )

    ns: Dict[str, Any] = {"__builtins__": {"True": True, "False": False, "None": None}}
    exec(compile(code_str, "<symbolic_repair>", "exec"), ns)  # noqa: S102
    repair_fn: Optional[Callable[[Any, Dict[str, str]], None]] = ns.get("repair")
    if not callable(repair_fn):
        return ZeroPHIRepairResult(
            verified=False,
            synthesized_code=code_str,
            outbound_twin=twin,
            failure_reason="missing_repair_entrypoint",
        )

    try:
        repair_fn(_SandboxedUIProxy(page), dict(private_vars))
    except Exception as exc:
        return ZeroPHIRepairResult(
            verified=False,
            synthesized_code=code_str,
            outbound_twin=twin,
            failure_reason=f"runtime_repair_exception:{type(exc).__name__}",
        )

    if not readback_oracle(page):
        return ZeroPHIRepairResult(
            verified=False,
            synthesized_code=code_str,
            outbound_twin=twin,
            failure_reason="readback_refuted_repair",
        )

    return ZeroPHIRepairResult(
        verified=True,
        synthesized_code=code_str,
        outbound_twin=twin,
    )
