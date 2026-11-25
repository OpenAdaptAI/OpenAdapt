"""Zero-PHI Structural Twin and AST-Sandboxed Frontier Coding-Agent Runtime Repair."""

from __future__ import annotations

import ast
import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

from openadapt_kernel.macro import ControlSchema, observe_schema

_FROZEN_UI_VOCAB: FrozenSet[str] = frozenset(
    "add advanced all amount and apply auth authorization back bind body button cancel "
    "categories category check checkbox choose claim clear close code confirm continue "
    "cookbook cookbooks coverage create date days deductible delete description detail "
    "details dialog disabled dob due edit email enable enabled error field filter filters "
    "first form hide home households id in inbox input is item items label labels last list "
    "login main member memo menu modal name new next no note notes number of ok one open "
    "option options or other outpatient overview page password phone plan policy previous "
    "primary priority project projects public publish query quote radio recipe recipes ref "
    "reference remove request required reset review row rule rules save search select send "
    "set settings show sign slug state status step submit tab tag tags task tasks team teams "
    "text textarea tier title to toggle type up upcoming update user username value view yes".split()
)

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_ALLOWED_KEYS: FrozenSet[str] = frozenset({"Enter", "Tab", "Escape", "Backspace", "ArrowDown", "ArrowUp", "Space"})
_ALLOWED_UI_METHODS: FrozenSet[str] = frozenset({
    "click", "click_first_option", "fill", "fill_var", "fill_var_quoted",
    "reconcile_stripped_var_in_detail", "select", "check", "press", "clear", "wait", "remove_idx",
})
_FORBIDDEN_AST_NODES = (
    ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.With, ast.AsyncWith, ast.Try,
    ast.Raise, ast.Delete, ast.Lambda, ast.ClassDef, ast.For, ast.AsyncFor, ast.While, ast.If,
    ast.IfExp, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.JoinedStr,
    ast.Assign, ast.AugAssign, ast.AnnAssign, ast.BinOp, ast.BoolOp, ast.Compare, ast.Await,
    ast.Yield, ast.YieldFrom,
)
_SAFE_TYPES = ("text", "checkbox", "radio", "submit", "button", "number", "date", "")
_SAFE_ROLES = ("dialog", "button", "textbox", "combobox", "checkbox", "listbox", "option", "")


def sanitize_ui_label_allowlist(raw: str) -> str:
    """Keep ONLY words in _FROZEN_UI_VOCAB; replace every other token with <OPAQUE>."""
    collapsed: List[str] = []
    for tok in _WORD_RE.findall(raw or "")[:12]:
        item = tok.lower() if tok.lower() in _FROZEN_UI_VOCAB else "<OPAQUE>"
        if not (item == "<OPAQUE>" and collapsed and collapsed[-1] == "<OPAQUE>"):
            collapsed.append(item)
    return " ".join(collapsed)


def export_zero_phi_dom_twin(controls: Sequence[ControlSchema]) -> List[Dict[str, Any]]:
    """Project live controls into a zero-PHI structural skeleton."""
    return [
        {
            "idx": c.control_index, "tag": c.tag,
            "type": c.input_type if c.input_type in _SAFE_TYPES else "other",
            "role": c.role if c.role in _SAFE_ROLES else "",
            "in_modal": bool(c.in_modal), "disabled": bool(c.disabled),
            "has_value": bool(c.value and c.value not in ("false", "")),
            "ui_chrome_tokens": sanitize_ui_label_allowlist(f"{c.label} {c.placeholder}"),
        }
        for c in controls
    ]


@dataclass
class ASTVerificationResult:
    ok: bool
    violations: List[str] = field(default_factory=list)


@dataclass
class DistilledRepairStep:
    method: str
    selector: str = ""
    modal_ordinal: int = -1
    var_key: str = ""
    arg: Any = None
    is_stripped_target: bool = False
    ctrl_sig: Tuple[str, str, str, str] = ("", "", "", "")


def verify_symbolic_repair_ast(code_str: str, allowed_vars: FrozenSet[str]) -> ASTVerificationResult:
    """Statically verify synthesized Python code invokes ONLY `ui.<allowed>` with symbolic vars."""
    try:
        tree = ast.parse(code_str, mode="exec")
    except SyntaxError as exc:
        return ASTVerificationResult(ok=False, violations=[f"syntax_error:{exc.msg}"])

    violations: List[str] = []
    allowed_strings = allowed_vars | _ALLOWED_KEYS
    for node in ast.walk(tree):
        if isinstance(node, _FORBIDDEN_AST_NODES):
            violations.append(f"forbidden_syntax:{type(node).__name__}")
        elif isinstance(node, ast.FunctionDef) and (node.name != "repair" or node.decorator_list):
            violations.append(f"forbidden_function_name:{node.name}")
        elif isinstance(node, ast.UnaryOp) and not (isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, int)):
            violations.append("forbidden_unary_op")
        elif isinstance(node, ast.Name) and (node.id.startswith("__") or node.id not in ("repair", "ui", "vars", "True", "False", "None")):
            violations.append(f"forbidden_identifier:{node.id}")
        elif isinstance(node, ast.Attribute) and (node.attr.startswith("_") or not isinstance(node.value, ast.Name) or node.value.id != "ui" or node.attr not in _ALLOWED_UI_METHODS):
            violations.append(f"forbidden_attribute:{ast.unparse(node)}")
        elif isinstance(node, ast.Call):
            if node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
                violations.append("forbidden_call_kwargs")
            if not (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "ui" and node.func.attr in _ALLOWED_UI_METHODS):
                violations.append(f"forbidden_call:{ast.unparse(node.func)}")
        elif isinstance(node, ast.Subscript):
            if not (isinstance(node.value, ast.Name) and node.value.id == "vars"):
                violations.append("forbidden_subscript_target")
            elif not (isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
                violations.append("non_constant_var_key")
            elif node.slice.value not in allowed_vars:
                violations.append(f"unknown_symbolic_var:{node.slice.value}")
        elif isinstance(node, ast.Constant) and (isinstance(node.value, bytes) or (isinstance(node.value, str) and node.value not in allowed_strings)):
            violations.append("forbidden_literal_constant")
    return ASTVerificationResult(ok=not violations, violations=violations)


class _SymVal(str):
    var_key: str

    def __new__(cls, key: str, val: str) -> "_SymVal":
        obj = super().__new__(cls, val)
        obj.var_key = key
        return obj


def _safe_selector(sel: str) -> str:
    m = re.search(r':has-text\("([^"]*)"\)', sel) if sel and ":has-text(" in sel else None
    return "" if (m and any(t.lower() not in _FROZEN_UI_VOCAB for t in _WORD_RE.findall(m.group(1)))) else sel


def _find_stripped_var_control_idx(controls: Sequence[ControlSchema], private_vars: Dict[str, str], preferred_var: str = "") -> Tuple[Optional[int], str]:
    pairs = [(preferred_var, private_vars[preferred_var])] if preferred_var in private_vars else list(private_vars.items())
    for var_key, secret_val in pairs:
        toks = [t.lower() for t in _WORD_RE.findall(secret_val) if len(t) >= 3]
        if len(toks) < 2:
            continue
        for c in controls:
            cand_raw = f"{c.label} {c.value}".strip() if c.tag not in ("textarea", "button") else ""
            if not cand_raw or secret_val in cand_raw:
                continue
            cand_toks = {t.lower() for t in _WORD_RE.findall(cand_raw)}
            if max(2, (len(toks) + 1) // 2) <= sum(1 for t in toks if t in cand_toks) < len(toks):
                return c.control_index, var_key
    return None, ""


def _has_disabled_submit(controls: Sequence[ControlSchema]) -> bool:
    return any(c.disabled and (c.input_type == "submit" or sanitize_ui_label_allowlist(c.label) in ("save", "create", "submit")) for c in ([x for x in controls if x.in_modal] or list(controls)))


_ANCESTOR_BOX_JS = """el => {
  for (let cur = el, d = 0; d < 6 && cur; d++, cur = cur.parentElement) {
    const st = window.getComputedStyle(cur), r = cur.getBoundingClientRect();
    if (st && st.display !== 'none' && st.visibility !== 'hidden' && parseFloat(st.opacity || '1') > 0.01 && r.width >= 4 && r.height >= 4) {
      cur.scrollIntoView({ block: 'center', inline: 'nearest' }); const r2 = cur.getBoundingClientRect();
      return { x: r2.left + r2.width / 2, y: r2.top + r2.height / 2, found: true };
    }
  }
  try { el.focus(); el.click(); } catch (e) {} return { x: 0, y: 0, found: false };
}"""
_FIRST_OPTION_BOX_JS = """() => {
  const sels = ['.v-overlay--active [role="option"], .menuable__content__active [role="option"], [role="dialog"] [role="option"]', '[role="option"]:not(a[href])', '.menuable__content__active .v-list-item, .v-overlay--active .v-list-item, [role="menu"] [role="menuitem"]'];
  for (const sel of sels) for (const el of document.querySelectorAll(sel)) {
    const st = window.getComputedStyle(el), r = el.getBoundingClientRect();
    if (st && st.display !== 'none' && st.visibility !== 'hidden' && r.width >= 4 && r.height >= 4) {
      el.scrollIntoView({ block: 'nearest' }); const r2 = el.getBoundingClientRect();
      return { x: r2.left + r2.width / 2, y: r2.top + r2.height / 2, found: true };
    }
  }
  return { x: 0, y: 0, found: false };
}"""


class _ZeroPhiUIProxy:
    """Executes ONLY indexed symbolic UI primitives on the local Playwright page."""

    def __init__(
        self, page: Any, private_vars: Optional[Dict[str, str]] = None,
        controls: Optional[Sequence[ControlSchema]] = None,
        stripped_idx: Optional[int] = None, stripped_var: str = "",
    ) -> None:
        self._page = page
        self._vars = dict(private_vars or {})
        self._by_idx: Dict[int, ControlSchema] = {c.control_index: c for c in (controls or [])}
        self._modal_ords: Dict[int, int] = {
            c.control_index: i for i, c in enumerate([x for x in (controls or []) if x.in_modal and x.role != "option"])
        }
        self._stripped_idx, self._stripped_var = stripped_idx, stripped_var
        self.recorded_steps: List[DistilledRepairStep] = []

    def _resolve_var(self, val: Any) -> Tuple[str, str]:
        vkey = getattr(val, "var_key", "")
        if vkey and vkey in self._vars:
            return vkey, self._vars[vkey]
        sval = str(val)
        return (sval, self._vars[sval]) if sval in self._vars else ("", sval)

    def _raw_loc(self, idx: int) -> Any:
        loc = self._page.locator(f'[data-oa-idx="{int(idx)}"]').first
        if loc.count() == 0:
            for c in observe_schema(self._page):
                self._by_idx.setdefault(c.control_index, c)
            loc = self._page.locator(f'[data-oa-idx="{int(idx)}"]').first
        if loc.count() == 0:
            raise LookupError(f"target_not_found:{idx}")
        return loc

    def _is_visible(self, loc: Any, timeout_ms: int = 1500) -> bool:
        try:
            loc.wait_for(state="visible", timeout=timeout_ms)
            return True
        except Exception:
            return False

    def _activate_composite(self, loc: Any) -> None:
        box = loc.evaluate(_ANCESTOR_BOX_JS)
        if box.get("found") and hasattr(self._page, "mouse"):
            self._page.mouse.click(float(box["x"]), float(box["y"]))
        loc.evaluate("el => { try { el.focus(); } catch (e) {} }")

    def _record(self, method: str, idx: int = -1, var_key: str = "", arg: Any = None) -> None:
        c = self._by_idx.get(int(idx))
        is_strip = idx >= 0 and self._stripped_idx is not None and int(idx) == self._stripped_idx
        sig = (c.tag, c.input_type, c.role, sanitize_ui_label_allowlist(f"{c.label} {c.placeholder}")) if c else ("", "", "", "")
        self.recorded_steps.append(DistilledRepairStep(method, _safe_selector(c.selector) if c else "", self._modal_ords.get(int(idx), -1), var_key or (self._stripped_var if is_strip else ""), arg, is_strip, sig))

    def click(self, idx: int) -> None:
        loc, is_dis = self._raw_loc(idx), "el => Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true')"
        for _ in range(2):
            if not loc.evaluate(is_dis):
                break
            self._page.wait_for_timeout(250)
        if loc.evaluate(is_dis):
            raise PermissionError("submit_disabled")
        clicked = False
        if self._is_visible(loc, 1500):
            try:
                loc.scroll_into_view_if_needed()
                loc.click(timeout=1500)
                clicked = True
            except Exception:
                clicked = False
        if not clicked:
            self._activate_composite(loc)
        self._page.wait_for_timeout(250)
        self._record("click", idx=int(idx))

    def _fill_impl(self, idx: int, text_val: str, method_name: str, var_key: str) -> None:
        loc, filled = self._raw_loc(idx), False
        if self._is_visible(loc, 1500):
            try:
                loc.scroll_into_view_if_needed()
                loc.fill(text_val, timeout=1500)
                filled = True
            except Exception:
                inner = loc.locator('input, textarea, [contenteditable="true"]').first
                if inner.count() > 0:
                    self._activate_composite(loc)
                    try:
                        inner.fill(text_val, timeout=1000)
                        filled = True
                    except Exception:
                        pass
        if not filled:
            self._activate_composite(loc)
            self._page.wait_for_timeout(120)
            try:
                loc.fill(text_val, timeout=1000)
            except Exception:
                self._page.keyboard.insert_text(text_val)
        self._page.wait_for_timeout(150)
        self._record(method_name, idx=int(idx), var_key=var_key)

    def fill(self, idx: int, val: str) -> None:
        vk, tv = self._resolve_var(val)
        self._fill_impl(idx, tv, "fill_var" if vk else "fill", vk)

    def fill_var(self, idx: int, var_name: str) -> None:
        vk, tv = self._resolve_var(var_name)
        self._fill_impl(idx, tv, "fill_var", vk)

    def fill_var_quoted(self, idx: int, var_name: str) -> None:
        vk, tv = self._resolve_var(var_name)
        self._fill_impl(idx, f'"{tv}"', "fill_var_quoted", vk)

    def clear(self, idx: int) -> None:
        self._fill_impl(idx, "", "clear", "")

    def check(self, idx: int, state: bool = True) -> None:
        loc = self._raw_loc(idx)
        try:
            loc.check(timeout=1500) if state else loc.uncheck(timeout=1500)
        except Exception:
            loc.check(force=True, timeout=1000) if state else loc.uncheck(force=True, timeout=1000)
        self._page.wait_for_timeout(150)
        self._record("check", idx=int(idx), arg=bool(state))

    def select(self, idx: int, val: str) -> None:
        vk, tv = self._resolve_var(val)
        self._raw_loc(idx).select_option(tv, timeout=2000)
        self._record("select", idx=int(idx), var_key=vk)

    def press(self, idx: int, key: str) -> None:
        if key not in _ALLOWED_KEYS:
            raise ValueError(f"Disallowed key: {key}")
        loc, ok = self._raw_loc(idx), False
        if self._is_visible(loc, 1500):
            try:
                loc.press(key, timeout=1500)
                ok = True
            except Exception:
                ok = False
        if not ok:
            self._activate_composite(loc)
            self._page.keyboard.press(key)
        self._page.wait_for_timeout(150)
        self._record("press", idx=int(idx), arg=key)

    def click_first_option(self) -> None:
        for _ in range(6):
            box = self._page.evaluate(_FIRST_OPTION_BOX_JS)
            if box.get("found") and hasattr(self._page, "mouse"):
                self._page.mouse.click(float(box["x"]), float(box["y"]))
                self._page.wait_for_timeout(250)
                self._record("click_first_option")
                return
            self._page.wait_for_timeout(250)
        raise LookupError("no_active_option_visible")

    def reconcile_stripped_var_in_detail(self, detail_trigger_idx: int, title_idx: int, var_name: str) -> None:
        vkey, exact_val = self._resolve_var(var_name)
        saved_len = len(self.recorded_steps)
        self.click(detail_trigger_idx)
        del self.recorded_steps[saved_len:]
        self._page.wait_for_timeout(650)
        detail_ctrls = observe_schema(self._page)
        editable = lambda c: c.tag in ("input", "textarea") or c.role == "textbox"  # noqa: E731
        target_ctrl = next((c for c in detail_ctrls if c.control_index == int(title_idx) and editable(c)), None) if int(title_idx) >= 0 else None
        if target_ctrl is None:
            strip_i, _ = _find_stripped_var_control_idx(detail_ctrls, {vkey or "$v": exact_val})
            if strip_i is not None:
                target_ctrl = next((c for c in detail_ctrls if c.control_index == strip_i and editable(c)), None)
        if target_ctrl is None:
            target_ctrl = next((c for c in detail_ctrls if c.role == "textbox" or (c.tag == "input" and c.input_type in ("text", ""))), None)
        if target_ctrl is None:
            raise LookupError("detail_title_control_not_found")
        t_loc = self._page.locator(f'[data-oa-idx="{target_ctrl.control_index}"]').first
        t_loc.click(timeout=1500)
        t_loc.fill(exact_val, timeout=1500)
        t_loc.press("Enter", timeout=1500)
        t_loc.evaluate("el => { try { el.blur(); } catch (e) {} }")
        self._page.wait_for_timeout(500)
        self._record("reconcile_stripped_var_in_detail", idx=int(detail_trigger_idx), var_key=vkey, arg=int(title_idx))

    def remove_idx(self, idx: int) -> None:
        self.click(idx)

    def wait(self, ms: int) -> None:
        clamped = min(max(int(ms), 0), 3000)
        self._page.wait_for_timeout(clamped)
        self._record("wait", arg=clamped)


_SandboxedUIProxy = _ZeroPhiUIProxy


@dataclass
class ZeroPHIRepairResult:
    verified: bool
    synthesized_code: str = ""
    outbound_twin: List[Dict[str, Any]] = field(default_factory=list)
    ast_violations: List[str] = field(default_factory=list)
    failure_reason: str = ""
    turns_used: int = 0
    turn_history: List[Dict[str, Any]] = field(default_factory=list)
    distilled_steps: List[DistilledRepairStep] = field(default_factory=list)


def _accepts_feedback_arg(fn: Callable[..., str]) -> bool:
    try:
        ps = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    return any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in ps) or sum(1 for p in ps if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)) >= 4


def _verify_oracle_with_settle(page: Any, readback_oracle: Callable[[Any], bool]) -> bool:
    if readback_oracle(page):
        return True
    if hasattr(page, "wait_for_timeout"):
        page.wait_for_timeout(350)
    return bool(readback_oracle(page))


def execute_zero_phi_coding_repair(page: Any, *, private_vars: Dict[str, str], structural_error: str, coding_agent_fn: Callable[..., str], readback_oracle: Callable[[Any], bool], max_turns: int = 3) -> ZeroPHIRepairResult:
    """Run bounded multi-turn Zero-PHI Structural Twin -> Coding Agent -> AST Firewall -> Read-Back."""
    var_keys, safe_error = sorted(private_vars.keys()), sanitize_ui_label_allowlist(structural_error)
    wants_feedback, limit = _accepts_feedback_arg(coding_agent_fn), max(1, int(max_turns))
    prev_sigs: set[Tuple[Any, ...]] = set()
    last_failure_code, last_ast_violations = "", []
    history, executed_codes, accumulated_steps, twin = [], [], [], []

    for turn_idx in range(1, limit + 1):
        controls = observe_schema(page)
        twin = export_zero_phi_dom_twin(controls)
        cur_sigs = {(t["tag"], t["type"], t["role"], t["in_modal"], t["ui_chrome_tokens"]) for t in twin}
        newly_mounted = [
            t for t in twin if (t["tag"], t["type"], t["role"], t["in_modal"], t["ui_chrome_tokens"]) not in prev_sigs or t["role"] == "option"
        ] if turn_idx > 1 else []
        prev_sigs = cur_sigs
        submit_disabled = _has_disabled_submit(controls)
        stripped_idx, stripped_var = _find_stripped_var_control_idx(controls, private_vars)
        feedback: Dict[str, Any] = {
            "turn": turn_idx, "max_turns": limit, "submit_still_disabled": submit_disabled,
            "active_option_count": sum(1 for c in controls if c.role == "option"),
            "newly_mounted_controls": newly_mounted,
            "readback_status": "var_token_stripped_on_quick_input" if stripped_idx is not None else ("submit_disabled" if submit_disabled else "missing_expected_token_or_unchanged"),
            "stripped_item_idx": stripped_idx, "last_failure_code": last_failure_code,
        }
        code_str = coding_agent_fn(twin, var_keys, safe_error, feedback) if wants_feedback else coding_agent_fn(twin, var_keys, safe_error)
        ast_check = verify_symbolic_repair_ast(code_str, frozenset(var_keys))
        if not ast_check.ok:
            last_failure_code, last_ast_violations = "ast_firewall_rejected", ast_check.violations
            history.append({"turn": turn_idx, "feedback": feedback, "code": code_str, "outcome": last_failure_code})
            if not wants_feedback or turn_idx == limit:
                return ZeroPHIRepairResult(False, code_str, twin, last_ast_violations, "ast_firewall_rejected", turn_idx, history)
            continue

        ns: Dict[str, Any] = {"__builtins__": {"True": True, "False": False, "None": None}}
        exec(compile(code_str, "<symbolic_repair>", "exec"), ns)  # noqa: S102
        repair_fn = ns.get("repair")
        if not callable(repair_fn):
            return ZeroPHIRepairResult(False, code_str, twin, [], "missing_repair_entrypoint", turn_idx, history)

        proxy = _ZeroPhiUIProxy(page, private_vars, controls, stripped_idx, stripped_var)
        executed_codes.append(code_str)
        try:
            repair_fn(proxy, {k: _SymVal(k, v) for k, v in private_vars.items()})
        except Exception as exc:
            is_sub = isinstance(exc, PermissionError)
            last_failure_code = "submit_disabled" if is_sub else ("target_not_found" if isinstance(exc, LookupError) else ("action_timeout" if "Timeout" in type(exc).__name__ else "runtime_exception"))
            accumulated_steps.extend(proxy.recorded_steps)
            history.append({"turn": turn_idx, "feedback": feedback, "code": code_str, "outcome": last_failure_code})
            if not wants_feedback or turn_idx == limit:
                reason = "runtime_repair_exception:SubmitDisabled" if is_sub else f"runtime_repair_exception:{type(exc).__name__}"
                return ZeroPHIRepairResult(False, code_str, twin, [], reason, turn_idx, history)
            continue

        accumulated_steps.extend(proxy.recorded_steps)
        if _verify_oracle_with_settle(page, readback_oracle):
            history.append({"turn": turn_idx, "feedback": feedback, "code": code_str, "outcome": "verified"})
            return ZeroPHIRepairResult(True, "\n\n# --- turn ---\n".join(executed_codes), twin, [], "", turn_idx, history, accumulated_steps)
        last_failure_code = "readback_refuted"
        history.append({"turn": turn_idx, "feedback": feedback, "code": code_str, "outcome": last_failure_code})
        if not wants_feedback:
            break

    return ZeroPHIRepairResult(False, "\n\n# --- turn ---\n".join(executed_codes), twin, last_ast_violations, "readback_refuted_repair", len(history), history)


def replay_distilled_zero_phi_repair(
    page: Any, distilled_steps: Sequence[DistilledRepairStep], *,
    private_vars: Dict[str, str], readback_oracle: Callable[[Any], bool],
) -> bool:
    """Replay a distilled Zero-PHI symbolic repair trace locally with zero cloud calls."""
    for step in distilled_steps:
        controls = observe_schema(page)
        stripped_idx, stripped_var = _find_stripped_var_control_idx(controls, private_vars, preferred_var=step.var_key)
        proxy = _ZeroPhiUIProxy(page, private_vars, controls, stripped_idx, stripped_var)
        if step.method == "wait":
            proxy.wait(int(step.arg or 0))
            continue
        if step.method == "click_first_option":
            proxy.click_first_option()
            continue
        target_idx: Optional[int] = stripped_idx if (step.is_stripped_target and stripped_idx is not None) else None
        if target_idx is None and step.modal_ordinal >= 0:
            modal_ctrls = [c for c in controls if c.in_modal and c.role != "option"]
            csig = lambda x: (x.tag, x.input_type, x.role, sanitize_ui_label_allowlist(f"{x.label} {x.placeholder}"))  # noqa: E731
            if step.modal_ordinal < len(modal_ctrls) and (step.ctrl_sig == ("", "", "", "") or csig(modal_ctrls[step.modal_ordinal]) == step.ctrl_sig):
                target_idx = modal_ctrls[step.modal_ordinal].control_index
            else:
                hits = [i for i, x in enumerate(modal_ctrls) if csig(x) == step.ctrl_sig]
                if hits:
                    target_idx = modal_ctrls[min(hits, key=lambda i: abs(i - step.modal_ordinal))].control_index
        if target_idx is None and step.selector:
            loc = page.locator(step.selector).first
            raw_idx = loc.get_attribute("data-oa-idx") if loc.count() > 0 else None
            target_idx = int(raw_idx) if raw_idx and raw_idx.isdigit() else None
        if target_idx is None:
            return False
        dispatch_fn: Optional[Callable[[int], None]] = {
            "click": proxy.click, "fill": lambda i: proxy.fill_var(i, step.var_key),
            "fill_var": lambda i: proxy.fill_var(i, step.var_key), "fill_var_quoted": lambda i: proxy.fill_var_quoted(i, step.var_key),
            "press": lambda i: proxy.press(i, str(step.arg)), "check": lambda i: proxy.check(i, bool(step.arg)),
            "clear": proxy.clear, "select": lambda i: proxy.select(i, step.var_key),
            "reconcile_stripped_var_in_detail": lambda i: proxy.reconcile_stripped_var_in_detail(i, int(step.arg if step.arg is not None else -1), step.var_key),
        }.get(step.method)
        if dispatch_fn is not None:
            dispatch_fn(target_idx)
    return _verify_oracle_with_settle(page, readback_oracle)
