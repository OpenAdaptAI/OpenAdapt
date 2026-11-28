"""Coding-Agent AppCartridge Compiler: Autoformalized SOPs, Zero-Guideline Probing, and Induction."""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from openadapt_kernel.canonical import parse_canonical_currency_cents
from openadapt_kernel.egress_guard import (
    BijectiveSymbolCodec,
    EgressBlockedError,
    EgressPolicy,
    LocalEgressGuard,
    OrderPreservingRankCodec,
    summarize_egress_ledger,
)
from openadapt_kernel.fsm import (
    PolicyDecisionTable,
    PolicyRule,
    _policy_value_matches,
    evaluate_policy_decision,
)
from openadapt_kernel.ledger import EpistemicArtifact, SpanAnchor, verify_span_anchor
from openadapt_kernel.stateful_twin import StatefulTwinApp
from openadapt_kernel.zero_phi_repair import verify_symbolic_repair_ast

_CARTRIDGE_EXTRA_TOKENS: FrozenSet[str] = frozenset(
    "adjustment appeal api artifact artifacts assignee attachment attachments auth authorization "
    "benefit benefits branch branches cartridge category claim claims cob coding context contract "
    "corrected correction coverage crm deductible denial deny disposition doc docs document "
    "documents duplicate eligible eob era escalate escalation evidence exceeded excluded exclusion "
    "expected expired explanation feature features fields filing follow followup get guideline "
    "guidelines har head hold if incomplete ineligible inquiry internal invalid json ledger limit "
    "method missing mismatch mod modifier modifiers mutant mutants network noncovered note notes "
    "observations off only option oracle oracles out override owner paid partial path pattern "
    "plan portal preauth prior priority quantity rate read reads rebilled records referral "
    "remittance reprocess request response responsibility resubmit resubmission retroactive review "
    "route routing rule rules schema schemas secondary skill skills sla slot slots sop spec "
    "specialist split summary support suspended then timely trace traces transfer triage underpaid "
    "unverified waived when with without writeoff "
    "chosen_disposition demo_traces filters note_template_slots observed_features readback_path "
    "readback_url selector url_pattern var_key visited_tabs actions".split()
)
_READ_ONLY_METHODS: FrozenSet[str] = frozenset({"GET", "HEAD", "OPTIONS"})
_CMP_RE = re.compile(r"^(<=|>=|!=|<|>)\s*(-?\d+(?:\.\d+)?)$")
_SOP_LINE_RE = re.compile(
    r"(?:^|\b)IF\s+(?P<conds>.+?)\s+THEN\s+disposition\s*=\s*(?P<disp>[A-Za-z0-9_-]+)"
    r"(?:\s+READ\s+(?P<read>[A-Za-z0-9_,-]+))?"
    r"(?:\s+NOTES\s+(?P<notes>[A-Za-z0-9_,-]+))?"
    r"(?:\s+PRIORITY\s+(?P<prio>-?\d+))?",
    re.IGNORECASE,
)
_EVIDENCE_AFFORDANCE_TOKENS: FrozenSet[str] = frozenset(
    "attachment attachments auth authorization benefit benefits clinical coverage detail details "
    "doc docs document documents eob era evidence file history inquiries inquiry ledger note notes "
    "pdf plan policy remittance summary supporting timeline".split()
)
_COMMIT_ACTION_EXCLUDES: FrozenSet[str] = frozenset(
    {"submit", "save", "delete", "logout", "cancel", "close", "reset", "cosign", "sign", "sidebar"}
)
_AFFORDANCE_STOPWORDS: FrozenSet[str] = frozenset(
    {"a", "an", "and", "by", "enter", "field", "for", "if", "in", "input", "is", "of", "on", "or",
     "please", "provide", "required", "select", "specify", "the", "to", "type", "value", "with"}
)
_TWIN_PROBE_SNAPSHOT_JS = """(selectSel) => {
  const selEl = document.querySelector(selectSel);
  const vis = (el) => {
    if (!el) return false;
    if (el.hidden || el.getAttribute('aria-hidden') === 'true') return false;
    const st = window.getComputedStyle(el);
    if (!st || st.display === 'none' || st.visibility === 'hidden') return false;
    let cur = el.parentElement;
    while (cur) {
      if (cur.hidden || cur.getAttribute('aria-hidden') === 'true') return false;
      const cst = window.getComputedStyle(cur);
      if (cst && (cst.display === 'none' || cst.visibility === 'hidden')) return false;
      cur = cur.parentElement;
    }
    return true;
  };
  const labelFor = (el) => {
    const aria = (el.getAttribute('aria-label') || '').trim();
    if (aria) return aria;
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl && vis(lbl)) return (lbl.innerText || lbl.textContent || '').trim();
    }
    const pLbl = el.closest('label');
    if (pLbl && vis(pLbl)) return (pLbl.innerText || pLbl.textContent || '').trim();
    const prev = el.previousElementSibling;
    if (prev && prev.tagName.toLowerCase() === 'label' && vis(prev)) {
      return (prev.innerText || prev.textContent || '').trim();
    }
    return (el.getAttribute('placeholder') || el.getAttribute('name') || el.id || '').trim();
  };
  const inputs = [];
  const q = 'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]), textarea, select';
  for (const el of document.querySelectorAll(q)) {
    if (el === selEl || !vis(el)) continue;
    const lbl = labelFor(el);
    if (!lbl) continue;
    const req = Boolean(el.required || el.getAttribute('aria-required') === 'true');
    inputs.push({ label: lbl, required: req });
  }
  const helps = [];
  for (const el of document.querySelectorAll('p, small, span, div, [role="note"], [role="alert"]')) {
    if (!vis(el)) continue;
    if (el.querySelector('input, select, textarea, button, p, small, div')) continue;
    if (el.closest('label, button, select, option')) continue;
    const txt = (el.innerText || el.textContent || '').trim();
    if (txt) helps.push(txt);
  }
  return { inputs, helps };
}"""


@dataclass(frozen=True)
class SOPBranchSpec:
    branch_id: str
    when: Dict[str, Any]
    disposition: str
    required_read_artifacts: Tuple[str, ...] = ()
    required_note_slots: Tuple[str, ...] = ()
    priority: int = 0
    unless_defeaters: Tuple[str, ...] = ()


@dataclass(frozen=True)
class DefeasibleVerdict:
    allowed: bool
    defeated: bool
    defeater_reason: str = ""
    requires_prepare: bool = False
    branch_id: str = ""


@dataclass(frozen=True)
class CompiledSOPSpec:
    spec_id: str
    decision_table: PolicyDecisionTable
    branches: Tuple[SOPBranchSpec, ...]
    consistency_ok: bool
    consistency_conflicts: Tuple[str, ...] = ()
    observed_feature_domains: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    branch_support_counts: Dict[str, int] = field(default_factory=dict)
    post_commit_obligations: Dict[str, Tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class RawDemoTrace:
    demo_id: str
    goal_text: str
    visited_views: Tuple[str, ...]
    dom_observations: Dict[str, str]
    committed_fields: Dict[str, str]
    post_commit_actions: Tuple[str, ...] = ()
    post_commit_url: str = ""


@dataclass(frozen=True)
class SkillMacroSpec:
    skill_id: str
    name: str
    description: str
    parameters: Tuple[str, ...]
    code: str
    expected_url_pattern: str = ""
    required_filters: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class APIOracleSpec:
    oracle_id: str
    method: str
    path_pattern: str
    expected_fields: Tuple[str, ...] = ()
    read_only: bool = True


@dataclass(frozen=True)
class AdversarialMutantSpec:
    mutant_id: str
    description: str
    dossier: Dict[str, Any]
    expected_disposition: str = ""
    expected_blocked: bool = False
    unread_artifacts: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AppCartridge:
    cartridge_id: str
    portal_origin: str
    sop_spec: CompiledSOPSpec
    skills: Tuple[SkillMacroSpec, ...]
    api_oracles: Tuple[APIOracleSpec, ...]
    mutants: Tuple[AdversarialMutantSpec, ...]
    egress_audit: Dict[str, Any]


@dataclass(frozen=True)
class CartridgeQualificationReport:
    qualified: bool
    passed_mutants: int
    total_mutants: int
    failed_mutant_ids: Tuple[str, ...] = ()
    ast_violations: Tuple[str, ...] = ()
    consistency_ok: bool = True


@dataclass(frozen=True)
class EnumAffordanceProbe:
    option_value: str
    revealed_inputs: Tuple[str, ...]
    revealed_help_text: str
    required_fields: Tuple[str, ...]


@dataclass(frozen=True)
class CounterfactualEnumVerdict:
    decision: Literal["ALLOW", "PREPARE_REQUIRED"]
    selected_option: str
    votes: Dict[str, int]
    agreement_ratio: float
    reason: str
    affordance_matched: bool = False


@dataclass(frozen=True)
class ReversibilityCertificate:
    selector: str
    certified: bool
    information_gain: bool
    state_involution_ok: bool
    mutating_requests: Tuple[str, ...] = ()
    readonly_requests: Tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class OODSupportVerdict:
    in_support: bool
    unseen_features: Dict[str, str] = field(default_factory=dict)
    requires_prepare: bool = False
    matched_branch_id: Optional[str] = None
    branch_support: int = 0
    reason: str = ""


def _cartridge_policy(policy: Optional[EgressPolicy] = None) -> EgressPolicy:
    base = policy or EgressPolicy()
    return EgressPolicy(
        allowed_tokens=base.allowed_tokens,
        include_html_aria_vocab=base.include_html_aria_vocab,
        include_clinical_vocab=base.include_clinical_vocab,
        extra_allowed_tokens=base.extra_allowed_tokens | _CARTRIDGE_EXTRA_TOKENS,
        allowed_var_keys=base.allowed_var_keys,
    )


def _make_guard(
    egress_policy: Optional[EgressPolicy],
    ledger_path: Optional[Union[str, Path]],
    prefix: str,
) -> LocalEgressGuard:
    ledger_file = (
        Path(ledger_path)
        if ledger_path is not None
        else Path(tempfile.mkdtemp(prefix=prefix)) / "outbound_egress_ledger.jsonl"
    )
    return LocalEgressGuard(ledger_file, policy=_cartridge_policy(egress_policy))


def _extract_json_dict(raw_text: str) -> Dict[str, Any]:
    text = (raw_text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    cands = [fenced.group(1)] if fenced else []
    cands.append(text)
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e > s:
        cands.append(text[s : e + 1])
    for cand in cands:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise ValueError("coding_agent_invalid_cartridge_json")


def _parse_numeric_constraint(raw: Any) -> Optional[Tuple[str, float]]:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return ("==", float(raw))
    if isinstance(raw, str):
        m = _CMP_RE.match(raw.strip())
        if m:
            return (m.group(1), float(m.group(2)))
        try:
            return ("==", float(raw.strip()))
        except ValueError:
            return None
    return None


def _constraints_compatible_pure(v1: Any, v2: Any) -> bool:
    c1, c2 = _parse_numeric_constraint(v1), _parse_numeric_constraint(v2)
    if c1 is not None and c2 is not None:
        lo, lo_inc, hi, hi_inc = float("-inf"), False, float("inf"), False
        neq: List[float] = []
        for op, num in (c1, c2):
            if op == "==":
                if num > lo or (num == lo and not lo_inc):
                    lo, lo_inc = num, True
                if num < hi or (num == hi and not hi_inc):
                    hi, hi_inc = num, True
            elif op == ">=" and num > lo:
                lo, lo_inc = num, True
            elif op == ">" and (num > lo or (num == lo and lo_inc)):
                lo, lo_inc = num, False
            elif op == "<=" and num < hi:
                hi, hi_inc = num, True
            elif op == "<" and (num < hi or (num == hi and hi_inc)):
                hi, hi_inc = num, False
            elif op == "!=":
                neq.append(num)
        if lo > hi:
            return False
        return (lo_inc and hi_inc and lo not in neq) if lo == hi else True

    s1 = {str(x).strip().lower() for x in v1} if isinstance(v1, (list, tuple, set)) else {str(v1).strip().lower()}
    s2 = {str(x).strip().lower() for x in v2} if isinstance(v2, (list, tuple, set)) else {str(v2).strip().lower()}
    if len(s1) == 1 and next(iter(s1)).startswith("!="):
        return any(x != next(iter(s1))[2:].strip() for x in s2)
    if len(s2) == 1 and next(iter(s2)).startswith("!="):
        return any(x != next(iter(s2))[2:].strip() for x in s1)
    return bool(s1 & s2)


def _predicates_satisfiable_together(w1: Mapping[str, Any], w2: Mapping[str, Any]) -> bool:
    shared = set(w1.keys()) & set(w2.keys())
    if not shared:
        return True
    try:
        z3 = importlib.import_module("z3")
    except ImportError:
        return all(_constraints_compatible_pure(w1[k], w2[k]) for k in shared)

    solver = z3.Solver()
    for k in shared:
        c1, c2 = _parse_numeric_constraint(w1[k]), _parse_numeric_constraint(w2[k])
        if c1 is not None and c2 is not None:
            var = z3.Real(k)
            for op, num in (c1, c2):
                if op == "==":
                    solver.add(var == num)
                elif op == "!=":
                    solver.add(var != num)
                elif op == ">=":
                    solver.add(var >= num)
                elif op == ">":
                    solver.add(var > num)
                elif op == "<=":
                    solver.add(var <= num)
                elif op == "<":
                    solver.add(var < num)
        elif not _constraints_compatible_pure(w1[k], w2[k]):
            return False
    return bool(solver.check() == z3.sat)


def check_sop_consistency(branches: Sequence[SOPBranchSpec]) -> Tuple[bool, Tuple[str, ...]]:
    """Verify no two same-priority SOP branches have overlapping conditions with conflicting dispositions."""
    conflicts: List[str] = []
    for i in range(len(branches)):
        for j in range(i + 1, len(branches)):
            b1, b2 = branches[i], branches[j]
            if b1.priority == b2.priority and b1.disposition.strip().lower() != b2.disposition.strip().lower():
                if _predicates_satisfiable_together(b1.when, b2.when):
                    conflicts.append(f"conflict:{b1.branch_id}({b1.disposition})_vs_{b2.branch_id}({b2.disposition})")
    return len(conflicts) == 0, tuple(conflicts)


def _build_compiled_sop(spec_id: str, branches: Sequence[SOPBranchSpec]) -> CompiledSOPSpec:
    ordered = tuple(sorted(branches, key=lambda b: (-int(b.priority), b.branch_id)))
    rules = tuple(
        PolicyRule(b.branch_id, {str(k): str(v) for k, v in b.when.items()}, b.disposition)
        for b in ordered
    )
    table = PolicyDecisionTable(table_id=spec_id, rules=rules, require_human_if_unmatched=True)
    ok, conflicts = check_sop_consistency(ordered)
    return CompiledSOPSpec(spec_id, table, ordered, ok, conflicts)


def _parse_branches_deterministic(sop_text: str) -> List[SOPBranchSpec]:
    branches: List[SOPBranchSpec] = []
    for idx, line in enumerate(sop_text.splitlines(), start=1):
        m = _SOP_LINE_RE.search(line.strip())
        if not m:
            continue
        conds = {
            k.strip(): v.strip()
            for part in re.split(r"\s+AND\s+", m.group("conds"), flags=re.IGNORECASE)
            if "=" in part
            for k, v in [part.split("=", 1)]
        }
        read_arts = tuple(a.strip() for a in (m.group("read") or "").split(",") if a.strip())
        note_slots = tuple(s.strip() for s in (m.group("notes") or "").split(",") if s.strip())
        prio = int(m.group("prio")) if m.group("prio") else 0
        branches.append(SOPBranchSpec(f"branch_{idx}", conds, m.group("disp").strip(), read_arts, note_slots, prio))
    return branches


def _parse_branch_dicts(raw_list: Sequence[Mapping[str, Any]]) -> List[SOPBranchSpec]:
    return [
        SOPBranchSpec(
            branch_id=str(item.get("branch_id") or f"branch_{i}"),
            when=dict(item.get("when") or {}),
            disposition=str(item.get("disposition", "")).strip(),
            required_read_artifacts=tuple(str(a) for a in (item.get("required_read_artifacts") or [])),
            required_note_slots=tuple(str(s) for s in (item.get("required_note_slots") or [])),
            priority=int(item.get("priority", 0)),
            unless_defeaters=tuple(str(d) for d in (item.get("unless_defeaters") or [])),
        )
        for i, item in enumerate(raw_list, start=1)
    ]


def compile_sop_handbook_to_rules(
    sop_text: str,
    *,
    spec_id: str = "compiled_sop",
    coding_agent_fn: Optional[Callable[[str], str]] = None,
    egress_policy: Optional[EgressPolicy] = None,
    ledger_path: Optional[Union[str, Path]] = None,
) -> CompiledSOPSpec:
    """Audit SOP text via LocalEgressGuard and compile into a symbolically checked CompiledSOPSpec."""
    guard = _make_guard(egress_policy, ledger_path, "oa_sop_egress_")
    audited_sop = re.sub(r"\bPRIORITY\s+-?\d{1,2}\b", "PRIORITY", sop_text, flags=re.IGNORECASE)
    verdict = guard.check_and_record(audited_sop, destination="sop_compiler")
    if not verdict.allowed:
        raise EgressBlockedError(verdict)

    if coding_agent_fn is not None:
        prompt = (
            "Compile this SOP into JSON {\"branches\": [{\"branch_id\": \"...\", \"when\": {...}, "
            "\"disposition\": \"...\", \"required_read_artifacts\": [...], \"required_note_slots\": [...], "
            f"\"priority\": 0}}]}}.\nSOP:\n{sop_text}\n"
        )
        branches = _parse_branch_dicts(_extract_json_dict(coding_agent_fn(prompt)).get("branches") or [])
    else:
        branches = _parse_branches_deterministic(sop_text)
    return _build_compiled_sop(spec_id, branches)


def synthesize_app_cartridge_with_coding_agent(
    *,
    cartridge_id: str,
    portal_origin: str,
    sanitized_portal_spec: Mapping[str, Any],
    sop_text: str,
    coding_agent_fn: Callable[[str], str],
    har_schemas: Sequence[Mapping[str, Any]] = (),
    egress_policy: Optional[EgressPolicy] = None,
    ledger_path: Optional[Union[str, Path]] = None,
    strict_ast: bool = False,
) -> AppCartridge:
    """Audit portal/SOP/HAR specs via LocalEgressGuard and synthesize a verified AppCartridge."""
    guard = _make_guard(egress_policy, ledger_path, "oa_cartridge_egress_")
    audited_sop = re.sub(r"\bPRIORITY\s+-?\d{1,2}\b", "PRIORITY", sop_text, flags=re.IGNORECASE)
    outbound = {
        "portal_spec": dict(sanitized_portal_spec),
        "sop_text": audited_sop,
        "har_schemas": [dict(h) for h in har_schemas],
    }
    verdict = guard.check_and_record(outbound, destination="app_cartridge_synthesizer")
    if not verdict.allowed:
        raise EgressBlockedError(verdict)

    prompt = (
        "Synthesize an AppCartridge JSON with keys sop_branches, skills, api_oracles, mutants.\n"
        "Skills must define def repair(ui, vars): using only allowed ui methods and symbolic vars.\n"
        f"All api_oracles must be read_only=true using GET/HEAD.\nInput:\n{json.dumps(outbound, sort_keys=True)}\n"
    )
    spec = _extract_json_dict(coding_agent_fn(prompt))
    branches = _parse_branch_dicts(spec.get("sop_branches") or []) or _parse_branches_deterministic(sop_text)
    compiled_sop = _build_compiled_sop(f"{cartridge_id}_sop", branches)

    skills: List[SkillMacroSpec] = []
    for i, s in enumerate(spec.get("skills") or [], start=1):
        params = tuple(str(p) for p in (s.get("parameters") or []))
        code = str(s.get("code", ""))
        if strict_ast:
            ast_res = verify_symbolic_repair_ast(code, frozenset(params))
            if not ast_res.ok:
                raise ValueError(f"unsafe_skill_ast:{','.join(ast_res.violations)}")
        skills.append(
            SkillMacroSpec(
                str(s.get("skill_id") or f"skill_{i}"),
                str(s.get("name", f"skill_{i}")),
                str(s.get("description", "")),
                params,
                code,
                str(s.get("expected_url_pattern", "")),
                {str(k): str(v) for k, v in (s.get("required_filters") or {}).items()},
            )
        )

    oracles: List[APIOracleSpec] = []
    seen_paths: set[str] = set()
    for i, o in enumerate(spec.get("api_oracles") or [], start=1):
        method = str(o.get("method", "GET")).upper()
        if not bool(o.get("read_only", True)) or method not in _READ_ONLY_METHODS:
            raise ValueError(f"api_oracle_must_be_read_only:{method}")
        path_pat = str(o.get("path_pattern", "/"))
        seen_paths.add(path_pat)
        oracles.append(
            APIOracleSpec(
                str(o.get("oracle_id") or f"oracle_{i}"),
                method,
                path_pat,
                tuple(str(f) for f in (o.get("expected_fields") or [])),
                True,
            )
        )
    for i, h in enumerate(har_schemas, start=1):
        h_method, h_path = str(h.get("method", "GET")).upper(), str(h.get("path_pattern", ""))
        if h_method in _READ_ONLY_METHODS and h_path and h_path not in seen_paths:
            seen_paths.add(h_path)
            oracles.append(
                APIOracleSpec(
                    f"har_oracle_{i}",
                    h_method,
                    h_path,
                    tuple(str(f) for f in (h.get("response_fields") or [])),
                    True,
                )
            )

    mutants = tuple(
        AdversarialMutantSpec(
            str(m.get("mutant_id") or f"mutant_{i}"),
            str(m.get("description", "")),
            dict(m.get("dossier") or {}),
            str(m.get("expected_disposition", "")),
            bool(m.get("expected_blocked", False)),
            tuple(str(a) for a in (m.get("unread_artifacts") or [])),
        )
        for i, m in enumerate(spec.get("mutants") or [], start=1)
    )
    entries = guard.read_ledger()
    audit = {
        **summarize_egress_ledger(entries),
        "sha256_chain_integrity_verified": guard.verify_ledger_integrity(),
        "entry_sha256s": [str(e.get("entry_sha256", "")) for e in entries],
    }
    return AppCartridge(cartridge_id, portal_origin, compiled_sop, tuple(skills), tuple(oracles), mutants, audit)


def qualify_app_cartridge_offline(
    cartridge: AppCartridge,
    *,
    twin_app: Optional[StatefulTwinApp] = None,
) -> CartridgeQualificationReport:
    """Qualify an AppCartridge offline: SOP consistency, AST safety, read-only oracles, and 100% mutant pass."""
    ast_violations: List[str] = []
    for skill in cartridge.skills:
        res = verify_symbolic_repair_ast(skill.code, frozenset(skill.parameters))
        if not res.ok:
            ast_violations.extend(f"{skill.skill_id}:{v}" for v in res.violations)
    for oracle in cartridge.api_oracles:
        if not oracle.read_only or oracle.method.upper() not in _READ_ONLY_METHODS:
            ast_violations.append(f"{oracle.oracle_id}:non_readonly_oracle")

    branch_by_id = {b.branch_id: b for b in cartridge.sop_spec.branches}
    failed_mutants: List[str] = []
    passed = 0
    for m in cartridge.mutants:
        facts = {str(k): str(v) for k, v in m.dossier.items()}
        dec = evaluate_policy_decision(cartridge.sop_spec.decision_table, facts, m.expected_disposition or "APPROVE")
        matched_branch = branch_by_id.get(dec.matched_rule_id or "")
        missing_reads = (
            set(matched_branch.required_read_artifacts) & set(m.unread_artifacts) if matched_branch else set()
        )
        epistemic_blocked, policy_blocked = bool(missing_reads), (not dec.allowed) or dec.requires_prepare
        if m.expected_blocked:
            ok = epistemic_blocked or policy_blocked
        else:
            ok = (not epistemic_blocked) and dec.allowed and (
                not m.expected_disposition
                or (dec.expected_disposition or "").strip().lower() == m.expected_disposition.strip().lower()
            )
        if ok:
            passed += 1
        else:
            failed_mutants.append(m.mutant_id)

    twin_ok = True
    if twin_app is not None:
        audit = twin_app.egress_audit()
        twin_ok = bool(audit.get("sha256_chain_integrity_verified")) and twin_app.live_network_requests_attempted == 0

    qualified = (
        cartridge.sop_spec.consistency_ok
        and not ast_violations
        and not failed_mutants
        and len(cartridge.mutants) > 0
        and twin_ok
    )
    return CartridgeQualificationReport(
        qualified, passed, len(cartridge.mutants), tuple(failed_mutants), tuple(ast_violations),
        cartridge.sop_spec.consistency_ok,
    )


def _humanize_ident(raw: str) -> str:
    words = [w for w in re.split(r"[_\-\s]+", str(raw or "").strip()) if w]
    return " ".join(words).capitalize() if words else ""


def _humanize_condition(key: str, val: Any) -> str:
    hkey = _humanize_ident(key)
    if isinstance(val, (list, tuple, set)):
        items = ", ".join(_humanize_ident(str(x)).lower() for x in val)
        return f"{hkey} is one of {items}"
    sval = str(val).strip()
    m = _CMP_RE.match(sval)
    if m:
        op_words = {
            ">=": "is at least", "<=": "is at most",
            ">": "is greater than", "<": "is less than", "!=": "is not",
        }
        return f"{hkey} {op_words[m.group(1)]} {m.group(2)}"
    if sval.startswith("!="):
        return f"{hkey} is not {_humanize_ident(sval[2:]).lower()}"
    return f"{hkey} is {_humanize_ident(sval).lower()}"


def render_sop_signoff_summary(
    sop_spec: CompiledSOPSpec,
    *,
    sample_mutants: Sequence[AdversarialMutantSpec] = (),
) -> Dict[str, Any]:
    """Render a plain-English manager sign-off summary, markdown table, and SHA-256 SOP hash."""
    canonical_payload = {
        "spec_id": sop_spec.spec_id,
        "consistency_ok": sop_spec.consistency_ok,
        "branches": [
            {
                "branch_id": b.branch_id,
                "when": {str(k): b.when[k] for k in sorted(b.when.keys())},
                "disposition": b.disposition,
                "required_read_artifacts": list(b.required_read_artifacts),
                "required_note_slots": list(b.required_note_slots),
                "priority": b.priority,
            }
            for b in sop_spec.branches
        ],
    }
    sop_hash = hashlib.sha256(
        json.dumps(canonical_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    rule_rows: List[Dict[str, Any]] = []
    md_lines = [
        "| Rule | If Conditions | Then Disposition | Must Read First | Required Note Fields |",
        "| --- | --- | --- | --- | --- |",
    ]
    for idx, b in enumerate(sop_spec.branches, start=1):
        if_str = (
            " and ".join(_humanize_condition(k, b.when[k]) for k in sorted(b.when.keys()))
            if b.when else "Any case"
        )
        disp_str = _humanize_ident(b.disposition)
        reads = [_humanize_ident(a) for a in b.required_read_artifacts]
        notes = [_humanize_ident(n) for n in b.required_note_slots]
        rule_rows.append(
            {
                "rule_number": idx,
                "branch_id": b.branch_id,
                "if_conditions": if_str,
                "then_disposition": disp_str,
                "disposition_code": b.disposition,
                "must_read_first": reads,
                "required_note_fields": notes,
            }
        )
        md_lines.append(
            f"| Rule {idx} | {if_str} | {disp_str} | "
            f"{', '.join(reads) or 'None'} | {', '.join(notes) or 'None'} |"
        )

    boundary_examples: List[Dict[str, Any]] = []
    if sample_mutants:
        for m in sample_mutants:
            facts_str = ", ".join(_humanize_condition(k, v) for k, v in sorted(m.dossier.items()))
            if m.expected_blocked and m.unread_artifacts:
                unread_str = ", ".join(_humanize_ident(a) for a in m.unread_artifacts)
                outcome = f"Stopped before commit until operator or agent reads {unread_str}"
            elif m.expected_blocked:
                outcome = "Routed to operator review queue"
            else:
                outcome = _humanize_ident(m.expected_disposition)
            boundary_examples.append(
                {"scenario": m.description or _humanize_ident(m.mutant_id), "case_facts": facts_str, "expected_outcome": outcome}
            )
    else:
        for b in sop_spec.branches:
            facts_str = ", ".join(_humanize_condition(k, v) for k, v in sorted(b.when.items())) or "Any case"
            boundary_examples.append(
                {
                    "scenario": f"Matches {_humanize_ident(b.branch_id)}",
                    "case_facts": facts_str,
                    "expected_outcome": _humanize_ident(b.disposition),
                }
            )

    return {
        "spec_id": sop_spec.spec_id,
        "sop_hash": sop_hash,
        "consistency_ok": sop_spec.consistency_ok,
        "rules": rule_rows,
        "boundary_examples": boundary_examples,
        "markdown_table": "\n".join(md_lines),
    }


def _unicode_tokens(text: str) -> List[str]:
    return re.findall(r"[^\W_]+", (text or "").casefold())


def _slugify_tokens(text: str) -> str:
    toks = _unicode_tokens(text)
    return "_".join(toks[:6])


def discover_case_evidence_tabs(
    dom_controls: Sequence[Mapping[str, Any]],
    *,
    current_url: str = "",
    visited_labels: Sequence[str] = (),
    enable_english_keyword_fallback: bool = True,
) -> Tuple[EpistemicArtifact, ...]:
    """Discover unread/read evidence tabs/disclosures structurally (language-agnostic) + optional keyword fallback."""
    visited_norm = {str(v).strip().casefold() for v in visited_labels if str(v).strip()}
    visited_slugs = {_slugify_tokens(str(v)) for v in visited_labels if _slugify_tokens(str(v))}
    cur_url_low = (current_url or "").strip().casefold()

    artifacts: List[EpistemicArtifact] = []
    seen_ids: set[str] = set()
    for ctrl in dom_controls:
        role = str(ctrl.get("role") or "").strip().lower()
        tag = str(ctrl.get("tag") or "").strip().lower()
        itype = str(ctrl.get("input_type") or ctrl.get("type") or "").strip().lower()
        aria_controls = str(ctrl.get("aria_controls") or ctrl.get("aria-controls") or "").strip()
        rev_cert = bool(ctrl.get("reversibility_certified", False))

        if itype == "submit" or role == "menuitem":
            continue
        structural_match = (
            role == "tab"
            or tag == "summary"
            or bool(aria_controls)
            or rev_cert
        )
        if not structural_match and role not in {"tab", "link", "button", "region"} and tag not in {"a", "button", "section"}:
            continue
        label = str(
            ctrl.get("label") or ctrl.get("aria_label") or ctrl.get("aria-label")
            or ctrl.get("text") or ctrl.get("name") or ""
        ).strip()
        href = str(ctrl.get("href") or "").strip()
        tokens = set(_unicode_tokens(f"{label} {href}"))
        if not tokens:
            continue
        if not structural_match:
            if not enable_english_keyword_fallback:
                continue
            if tokens & _COMMIT_ACTION_EXCLUDES:
                continue
            if not (tokens & _EVIDENCE_AFFORDANCE_TOKENS):
                continue
        slug = _slugify_tokens(label or href)
        if not slug:
            continue
        kind = role if role in {"tab", "link", "button", "region"} else ("summary" if tag == "summary" else ("link" if tag == "a" else "button"))
        art_id = str(ctrl.get("artifact_id") or f"{kind}:{slug}")
        if art_id in seen_ids:
            continue
        seen_ids.add(art_id)
        is_read = (
            label.casefold() in visited_norm
            or art_id.casefold() in visited_norm
            or slug in visited_slugs
            or bool(cur_url_low and href and href.casefold() in cur_url_low)
        )
        artifacts.append(
            EpistemicArtifact(
                artifact_id=art_id,
                kind=kind,
                label=label or slug,
                required_before_commit=True,
                read=is_read,
            )
        )
    return tuple(artifacts)


def _is_readonly_post_graphql(post_data: str, dirty_values: Sequence[str]) -> bool:
    body = (post_data or "").strip()
    if not body:
        return False
    low = body.lower()
    if "mutation" in low:
        return False
    if "query" not in low:
        return False
    return not any(dv and dv.lower() in low for dv in dirty_values)


def probe_reversible_evidence_controls_in_twin(
    twin_app: StatefulTwinApp,
    candidate_selectors: Sequence[str],
    *,
    page_path: str = "/",
) -> Tuple[ReversibilityCertificate, ...]:
    """Certify reversible evidence controls via read-only network check (incl. GraphQL POST-for-read), info gain, and involution."""
    from playwright.sync_api import sync_playwright

    norm_path = page_path if page_path.startswith("/") else f"/{page_path}"
    target_url = f"{twin_app.base_url}{norm_path}"
    form_digest_js = """() => {
      const vals = Array.from(document.querySelectorAll('input, textarea, select')).map(el =>
        `${el.name||el.id||el.tagName}:${el.type==='checkbox'||el.type==='radio'?el.checked:el.value}`
      );
      return vals.join('|');
    }"""
    visible_text_js = "() => (document.body && document.body.innerText ? document.body.innerText.trim() : '')"
    certs: List[ReversibilityCertificate] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for sel in candidate_selectors:
                page = browser.new_page()
                twin_app.install_on_page(page)
                page.goto(target_url, wait_until="domcontentloaded")
                init_text = str(page.evaluate(visible_text_js) or "")
                init_digest = str(page.evaluate(form_digest_js) or "")
                dirty_vals = [seg.split(":", 1)[1] for seg in init_digest.split("|") if ":" in seg and seg.split(":", 1)[1]]

                initial_tab_sel = ""
                try:
                    active_tab = page.locator('[role="tab"][aria-selected="true"]').first
                    if active_tab.count() > 0:
                        tid = active_tab.get_attribute("id")
                        initial_tab_sel = f"#{tid}" if tid else '[role="tab"][aria-selected="true"]'
                except Exception:
                    initial_tab_sel = ""

                mutating: List[str] = []
                readonly: List[str] = []

                def _on_req(req: Any, _mut: List[str] = mutating, _ro: List[str] = readonly, _dv: List[str] = dirty_vals) -> None:
                    meth = str(req.method or "GET").upper()
                    url = str(req.url or "")
                    if meth in _READ_ONLY_METHODS:
                        _ro.append(f"{meth} {url}")
                    elif meth == "POST":
                        pdata = str(req.post_data or "")
                        if _is_readonly_post_graphql(pdata, _dv):
                            _ro.append(f"POST(graphql-read) {url}")
                        else:
                            _mut.append(f"POST {url}")
                    else:
                        _mut.append(f"{meth} {url}")

                page.on("request", _on_req)
                loc = page.locator(sel).first
                loc.click()
                page.wait_for_timeout(150)
                post_text = str(page.evaluate(visible_text_js) or "")
                info_gain = post_text != init_text and len(post_text) >= len(init_text) * 0.5

                # State involution: return to initial tab / close disclosure / Escape
                try:
                    if initial_tab_sel and initial_tab_sel != sel:
                        page.locator(initial_tab_sel).first.click(timeout=500)
                    else:
                        tag = str(loc.evaluate("el => el.tagName.toLowerCase()"))
                        if tag == "summary":
                            loc.click(timeout=500)
                        else:
                            page.keyboard.press("Escape")
                    page.wait_for_timeout(80)
                except Exception:
                    pass
                involution_ok = str(page.evaluate(form_digest_js) or "") == init_digest
                certified = (not mutating) and info_gain and involution_ok
                reason = (
                    "certified_reversible"
                    if certified
                    else ("mutating_network_request" if mutating else ("no_information_gain" if not info_gain else "state_involution_failed"))
                )
                certs.append(ReversibilityCertificate(sel, certified, info_gain, involution_ok, tuple(mutating), tuple(readonly), reason))
                page.close()
        finally:
            browser.close()
    return tuple(certs)


def probe_enum_affordances_in_twin(
    twin_app: StatefulTwinApp,
    select_selector: str,
    candidate_options: Sequence[str],
    *,
    page_path: str = "/",
) -> Tuple[EnumAffordanceProbe, ...]:
    """Probe each candidate enum option non-committingly inside an offline StatefulTwinApp."""
    from playwright.sync_api import sync_playwright

    norm_path = page_path if page_path.startswith("/") else f"/{page_path}"
    target_url = f"{twin_app.base_url}{norm_path}"
    probes: List[EnumAffordanceProbe] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            twin_app.install_on_page(page)
            page.goto(target_url, wait_until="domcontentloaded")
            base_snap = page.evaluate(_TWIN_PROBE_SNAPSHOT_JS, select_selector)
            base_inputs = {str(item["label"]) for item in base_snap.get("inputs", [])}
            base_reqs = {str(item["label"]) for item in base_snap.get("inputs", []) if item.get("required")}
            base_helps = {str(h) for h in base_snap.get("helps", [])}

            for opt in candidate_options:
                page.goto(target_url, wait_until="domcontentloaded")
                loc = page.locator(select_selector).first
                tag_name = str(loc.evaluate("el => el.tagName.toLowerCase()"))
                if tag_name == "select":
                    loc.select_option(opt)
                else:
                    loc.click()
                    page.locator(f'[role="option"]:has-text("{opt}")').first.click()
                page.wait_for_timeout(25)
                after_snap = page.evaluate(_TWIN_PROBE_SNAPSHOT_JS, select_selector)

                revealed_in: List[str] = []
                req_fields: List[str] = []
                for item in after_snap.get("inputs", []):
                    lbl = str(item["label"])
                    if lbl not in base_inputs and lbl not in revealed_in:
                        revealed_in.append(lbl)
                    if item.get("required") and (lbl not in base_reqs or lbl not in base_inputs):
                        if lbl not in req_fields:
                            req_fields.append(lbl)
                new_helps = [str(h) for h in after_snap.get("helps", []) if str(h) not in base_helps]
                probes.append(
                    EnumAffordanceProbe(
                        option_value=opt,
                        revealed_inputs=tuple(revealed_in),
                        revealed_help_text=" ".join(new_helps).strip(),
                        required_fields=tuple(req_fields),
                    )
                )
        finally:
            browser.close()
    return tuple(probes)


def _content_tokens(text: str) -> FrozenSet[str]:
    return frozenset(t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _AFFORDANCE_STOPWORDS)


def _active_fact_tokens(context_facts: Mapping[str, Any]) -> FrozenSet[str]:
    toks: set[str] = set()
    for k, v in context_facts.items():
        if v is None or v is False:
            continue
        sv = str(v).strip()
        if not sv or sv.lower() in {"false", "none", "null", "0", ""}:
            continue
        toks.update(_content_tokens(str(k)))
        toks.update(_content_tokens(sv))
    return frozenset(toks)


def _call_contrast_fn(
    contrast_fn: Callable[..., str],
    sample_idx: int,
    rotated_opts: Sequence[str],
    context_facts: Mapping[str, Any],
    affordance_probes: Sequence[EnumAffordanceProbe],
) -> str:
    try:
        sig = inspect.signature(contrast_fn)
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
        pos = [
            p for p in sig.parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
    except (TypeError, ValueError):
        return str(contrast_fn(sample_idx, rotated_opts, context_facts, affordance_probes))

    if has_varargs or len(pos) >= 4:
        return str(contrast_fn(sample_idx, rotated_opts, context_facts, affordance_probes))
    if len(pos) == 3:
        return str(contrast_fn(rotated_opts, context_facts, affordance_probes))
    if len(pos) == 2:
        return str(contrast_fn(rotated_opts, context_facts))
    if len(pos) == 1:
        return str(contrast_fn(sample_idx))
    return str(contrast_fn())


def evaluate_counterfactual_enum_gate(
    candidate_options: Sequence[str],
    *,
    context_facts: Mapping[str, Any],
    affordance_probes: Sequence[EnumAffordanceProbe] = (),
    contrast_fn: Optional[Callable[..., str]] = None,
    k_samples: int = 3,
    min_agreement: float = 1.0,
) -> CounterfactualEnumVerdict:
    """Gate enum selection on K counterfactual agreement checks or deterministic affordance alignment."""
    opts = [str(o) for o in candidate_options if str(o).strip()]
    if not opts or k_samples < 1:
        return CounterfactualEnumVerdict("PREPARE_REQUIRED", "", {}, 0.0, "no_candidate_options", False)

    fact_toks = _active_fact_tokens(context_facts)
    probe_by_opt = {p.option_value: p for p in affordance_probes}

    def _affordance_hit(opt: str) -> Tuple[int, bool]:
        p = probe_by_opt.get(opt)
        opt_toks = _content_tokens(opt)
        if p is None:
            overlap = len(opt_toks & fact_toks)
            return overlap, False
        probe_surface = " ".join((*p.revealed_inputs, *p.required_fields, p.revealed_help_text))
        probe_toks = _content_tokens(probe_surface)
        for req_lbl in p.required_fields:
            req_toks = _content_tokens(req_lbl)
            if req_toks and not (req_toks & fact_toks):
                return -1, False
        probe_overlap = len(probe_toks & fact_toks)
        total = probe_overlap * 2 + len(opt_toks & fact_toks)
        return total, probe_overlap > 0

    votes: Dict[str, int] = {}
    valid_set = set(opts)
    if contrast_fn is not None:
        n = len(opts)
        for i in range(k_samples):
            rotated = opts[i % n :] + opts[: i % n]
            pick = _call_contrast_fn(contrast_fn, i, rotated, context_facts, affordance_probes).strip()
            key = pick if pick in valid_set else "<INVALID>"
            votes[key] = votes.get(key, 0) + 1
    else:
        scored = [(opt, *_affordance_hit(opt)) for opt in opts]
        best_score = max(s[1] for s in scored)
        winners = [s for s in scored if s[1] == best_score and s[1] > 0]
        if len(winners) == 1:
            votes[winners[0][0]] = k_samples
        else:
            for s in scored:
                if s[1] == best_score and s[1] > 0:
                    votes[s[0]] = 1
            return CounterfactualEnumVerdict(
                "PREPARE_REQUIRED", "", votes, 0.0, "ambiguous_or_unmatched_affordances", False
            )

    ranked = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))
    top_opt, top_cnt = ranked[0]
    is_tie = len(ranked) > 1 and ranked[1][1] == top_cnt
    ratio = round(top_cnt / float(k_samples), 4)
    _, aff_ok = _affordance_hit(top_opt) if top_opt in valid_set else (0, False)

    if (not is_tie) and top_opt in valid_set and ratio >= min_agreement:
        reason = "unanimous_counterfactual_agreement" if ratio == 1.0 else "counterfactual_threshold_met"
        return CounterfactualEnumVerdict("ALLOW", top_opt, votes, ratio, reason, aff_ok)
    return CounterfactualEnumVerdict(
        "PREPARE_REQUIRED", "", votes, ratio, f"counterfactual_disagreement:{top_cnt}/{k_samples}", aff_ok
    )


def _induce_minimal_branches(observations: Sequence[Dict[str, Any]]) -> List[SOPBranchSpec]:
    by_disp: Dict[str, List[Dict[str, Any]]] = {}
    disp_order: List[str] = []
    for obs in observations:
        d = obs["disposition"]
        if d not in by_disp:
            disp_order.append(d)
            by_disp[d] = []
        by_disp[d].append(obs)

    def _matches(conds: Mapping[str, Any], feats: Mapping[str, Any]) -> bool:
        return all(str(feats.get(k, "")).strip().lower() == str(v).strip().lower() for k, v in conds.items())

    branches: List[SOPBranchSpec] = []
    b_idx = 0
    for disp in disp_order:
        positives = list(by_disp[disp])
        negatives = [o for o in observations if o["disposition"].lower() != disp.lower()]
        uncovered = list(positives)

        while uncovered:
            seed = uncovered[0]
            seed_feats: Dict[str, Any] = dict(seed["features"])
            # Greedily select minimal feature conjunction separating uncovered positives from all negatives
            chosen: Dict[str, Any] = {}
            remaining_negs = [n for n in negatives if _matches(chosen, n["features"])]
            cand_keys = sorted(seed_feats.keys())

            while remaining_negs and cand_keys:
                best_k = ""
                best_score: Tuple[int, int, str] = (-1, -1, "")
                for k in cand_keys:
                    trial = {**chosen, k: seed_feats[k]}
                    elim = sum(1 for n in remaining_negs if not _matches(trial, n["features"]))
                    cov = sum(1 for p in uncovered if _matches(trial, p["features"]))
                    # Prefer eliminating more negatives first, then covering more positives in this disposition
                    cand_score = (elim, cov, f"~{k}")
                    if elim > 0 and (best_k == "" or (elim, cov) > (best_score[0], best_score[1])):
                        best_k = k
                        best_score = cand_score
                if not best_k:
                    break
                chosen[best_k] = seed_feats[best_k]
                cand_keys.remove(best_k)
                remaining_negs = [n for n in negatives if _matches(chosen, n["features"])]

            if not chosen and seed_feats:
                first_k = sorted(seed_feats.keys())[0]
                chosen = {first_k: seed_feats[first_k]}
            elif remaining_negs:
                chosen = dict(seed_feats)

            covered_now = [p for p in uncovered if _matches(chosen, p["features"])] or [seed]
            lgg_when = {
                k: seed_feats[k]
                for k in sorted(seed_feats.keys())
                if all(
                    str(p["features"].get(k, "")).strip().lower() == str(seed_feats[k]).strip().lower()
                    for p in covered_now
                )
            }
            final_when = lgg_when or chosen
            reads: List[str] = []
            notes: List[str] = []
            for p in covered_now:
                for r in p["reads"]:
                    if r not in reads:
                        reads.append(r)
                for nt in p["notes"]:
                    if nt not in notes:
                        notes.append(nt)

            b_idx += 1
            branches.append(
                SOPBranchSpec(
                    branch_id=f"induced_{b_idx}",
                    when=final_when,
                    disposition=disp,
                    required_read_artifacts=tuple(reads),
                    required_note_slots=tuple(notes),
                    priority=0,
                )
            )
            covered_ids = {id(p) for p in covered_now}
            uncovered = [p for p in uncovered if id(p) not in covered_ids]
    return branches


def induce_sop_from_shadow_traces(
    traces_or_resolutions: Sequence[Mapping[str, Any]],
    *,
    spec_id: str = "induced_sop",
    coding_agent_fn: Optional[Callable[[str], str]] = None,
    egress_policy: Optional[EgressPolicy] = None,
    ledger_path: Optional[Union[str, Path]] = None,
) -> CompiledSOPSpec:
    """Audit Zero-PHI operator resolutions via LocalEgressGuard and induce a verified CompiledSOPSpec."""
    normalized: List[Dict[str, Any]] = []
    meta_keys = {
        "disposition", "decision", "selected_option", "features", "when", "context_facts", "dossier",
        "observed_read_artifacts", "read_artifacts", "required_read_artifacts",
        "observed_note_slots", "note_slots", "required_note_slots",
    }
    for item in traces_or_resolutions:
        disp = str(
            item.get("disposition") or item.get("decision") or item.get("selected_option") or ""
        ).strip()
        if not disp:
            continue
        raw_feats = item.get("features") or item.get("when") or item.get("context_facts") or item.get("dossier")
        if isinstance(raw_feats, Mapping):
            feats = {str(k): str(v) for k, v in raw_feats.items()}
        else:
            feats = {str(k): str(v) for k, v in item.items() if k not in meta_keys}
        reads = [
            str(a) for a in (
                item.get("observed_read_artifacts")
                or item.get("read_artifacts")
                or item.get("required_read_artifacts")
                or ()
            )
        ]
        notes = [
            str(n) for n in (
                item.get("observed_note_slots")
                or item.get("note_slots")
                or item.get("required_note_slots")
                or ()
            )
        ]
        normalized.append({"features": feats, "disposition": disp, "reads": reads, "notes": notes})

    guard = _make_guard(egress_policy, ledger_path, "oa_sop_induce_egress_")
    verdict = guard.check_and_record({"observations": normalized}, destination="sop_trace_inducer")
    if not verdict.allowed:
        raise EgressBlockedError(verdict)

    if coding_agent_fn is not None:
        prompt = (
            "Induce minimal non-conflicting SOP branches as JSON {\"branches\": [{\"branch_id\": \"...\", "
            "\"when\": {...}, \"disposition\": \"...\", \"required_read_artifacts\": [...], "
            f"\"required_note_slots\": [...], \"priority\": 0}}]}}.\nObservations:\n{json.dumps(normalized, sort_keys=True)}\n"
        )
        branches = _parse_branch_dicts(_extract_json_dict(coding_agent_fn(prompt)).get("branches") or [])
    else:
        branches = _induce_minimal_branches(normalized)
    return _build_compiled_sop(spec_id, branches)


def _normalize_readback_path_pattern(raw_url: str) -> str:
    raw = (raw_url or "").strip()
    if not raw:
        return ""
    path = raw
    if "://" in raw:
        after_scheme = raw.split("://", 1)[1]
        path = "/" + after_scheme.split("/", 1)[1] if "/" in after_scheme else "/"
    path = path.split("?", 1)[0].split("#", 1)[0]
    segs = []
    for seg in path.split("/"):
        if not seg:
            continue
        if re.fullmatch(r"\d+|[0-9a-f]{8,}|[A-Z]{2,5}-\d+", seg):
            segs.append("*")
        else:
            segs.append(seg)
    return "/" + "/".join(segs)


def _synthesize_demo_skill_code(actions: Sequence[Mapping[str, Any]], params: Sequence[str]) -> str:
    lines = ["def repair(ui, vars):"]
    for act in actions:
        kind = str(act.get("action") or act.get("method") or "click").strip().lower()
        var_key = str(act.get("var_key") or act.get("field") or "").strip().lstrip("$")
        if kind in ("fill", "fill_var") and var_key and var_key in params:
            lines.append(f'    ui.fill_var(0, vars["{var_key}"])')
        elif kind == "select" and var_key and var_key in params:
            lines.append(f'    ui.select(0, vars["{var_key}"])')
        elif kind == "check":
            lines.append("    ui.check(0)")
        elif kind == "wait":
            lines.append("    ui.wait(200)")
        else:
            lines.append("    ui.click(0)")
    if len(lines) == 1:
        lines.append("    ui.click(0)")
    return "\n".join(lines) + "\n"


def compile_demo_traces_to_cartridge(
    demo_traces: Sequence[Mapping[str, Any]],
    *,
    cartridge_id: str,
    portal_origin: str,
    coding_agent_fn: Optional[Callable[[str], str]] = None,
    egress_policy: Optional[EgressPolicy] = None,
    ledger_path: Optional[Union[str, Path]] = None,
) -> AppCartridge:
    """Compile Zero-PHI demonstration traces into a full AppCartridge (SOP, skills, oracles, mutants)."""
    guard = _make_guard(egress_policy, ledger_path, "oa_demo_cartridge_egress_")
    normalized_demos: List[Dict[str, Any]] = []
    sop_observations: List[Dict[str, Any]] = []

    for item in demo_traces:
        visited_tabs = [
            str(t).strip()
            for t in (item.get("visited_tabs") or item.get("observed_read_artifacts") or item.get("read_artifacts") or ())
            if str(t).strip()
        ]
        raw_feats = item.get("observed_features") or item.get("features") or item.get("context_facts") or {}
        observed_features = {str(k): str(v) for k, v in raw_feats.items()} if isinstance(raw_feats, Mapping) else {}
        chosen_disp = str(
            item.get("chosen_disposition") or item.get("disposition") or item.get("decision") or ""
        ).strip()
        note_slots = [
            str(s).strip()
            for s in (item.get("note_template_slots") or item.get("observed_note_slots") or item.get("required_note_slots") or ())
            if str(s).strip()
        ]
        readback_url = str(item.get("readback_url") or item.get("readback_path") or "").strip()
        raw_actions = item.get("actions") or ()
        actions = [
            {str(k): v for k, v in a.items()} for a in raw_actions if isinstance(a, Mapping)
        ]
        norm_entry = {
            "visited_tabs": visited_tabs,
            "observed_features": observed_features,
            "chosen_disposition": chosen_disp,
            "note_template_slots": note_slots,
            "readback_url": _normalize_readback_path_pattern(readback_url),
            "actions": actions,
        }
        normalized_demos.append(norm_entry)
        if chosen_disp:
            sop_observations.append(
                {
                    "disposition": chosen_disp,
                    "features": observed_features,
                    "observed_read_artifacts": visited_tabs,
                    "observed_note_slots": note_slots,
                }
            )

    verdict = guard.check_and_record({"demo_traces": normalized_demos}, destination="demo_cartridge_compiler")
    if not verdict.allowed:
        raise EgressBlockedError(verdict)

    # 1. Induce SOP spec (with required_read_artifacts, required_note_slots, observed domains & branch support)
    base_sop = induce_sop_from_shadow_traces(
        sop_observations,
        spec_id=f"{cartridge_id}_sop",
        coding_agent_fn=coding_agent_fn,
        egress_policy=egress_policy,
        ledger_path=guard.ledger_path,
    )
    domain_sets: Dict[str, set[str]] = {}
    for obs in sop_observations:
        for fk, fv in obs["features"].items():
            domain_sets.setdefault(str(fk), set()).add(str(fv).strip().casefold())
    observed_domains = {k: tuple(sorted(vs)) for k, vs in domain_sets.items()}
    support_counts: Dict[str, int] = {}
    for branch in base_sop.branches:
        cnt = sum(
            1
            for obs in sop_observations
            if all(
                str(obs["features"].get(k, "")).strip().casefold() == str(v).strip().casefold()
                for k, v in branch.when.items()
            )
        )
        support_counts[branch.branch_id] = cnt
    sop_spec = CompiledSOPSpec(
        spec_id=base_sop.spec_id,
        decision_table=base_sop.decision_table,
        branches=base_sop.branches,
        consistency_ok=base_sop.consistency_ok,
        consistency_conflicts=base_sop.consistency_conflicts,
        observed_feature_domains=observed_domains,
        branch_support_counts=support_counts,
    )

    # 2. Lift reusable SkillMacroSpecs (verified by verify_symbolic_repair_ast)
    skills: List[SkillMacroSpec] = []
    seen_skill_keys: set[str] = set()
    for idx, demo in enumerate(normalized_demos):
        disp_key = _slugify_tokens(demo["chosen_disposition"]) or f"demo_{idx + 1}"
        if disp_key in seen_skill_keys:
            continue
        seen_skill_keys.add(disp_key)
        params: List[str] = []
        filters: Dict[str, str] = {}
        url_pat = ""
        for act in demo["actions"]:
            vk = str(act.get("var_key") or act.get("field") or "").strip().lstrip("$")
            if vk and vk not in params:
                params.append(vk)
            if act.get("url_pattern") and not url_pat:
                url_pat = str(act["url_pattern"])
            if isinstance(act.get("filters"), Mapping):
                for fk, fv in act["filters"].items():
                    filters[str(fk)] = str(fv)
        for slot in demo["note_template_slots"]:
            clean_slot = slot.lstrip("$")
            if clean_slot and clean_slot not in params:
                params.append(clean_slot)

        code = _synthesize_demo_skill_code(demo["actions"], params)
        ast_res = verify_symbolic_repair_ast(code, frozenset(params))
        if not ast_res.ok:
            code = "def repair(ui, vars):\n    ui.click(0)\n"
        skills.append(
            SkillMacroSpec(
                skill_id=f"skill_{disp_key}",
                name=f"execute_{disp_key}",
                description=f"Lifted skill macro for {disp_key}",
                parameters=tuple(params),
                code=code,
                expected_url_pattern=url_pat or demo["readback_url"],
                required_filters=filters,
            )
        )

    # 3. Induce read-only APIOracleSpecs from post-commit readback routes
    oracles: List[APIOracleSpec] = []
    seen_paths: set[str] = set()
    for demo in normalized_demos:
        path_pat = demo["readback_url"]
        if not path_pat or path_pat in seen_paths:
            continue
        seen_paths.add(path_pat)
        exp_fields = tuple(sorted({*demo["note_template_slots"], *demo["observed_features"].keys()}))
        oracles.append(
            APIOracleSpec(
                oracle_id=f"oracle_{len(oracles) + 1}",
                method="GET",
                path_pattern=path_pat,
                expected_fields=exp_fields,
                read_only=True,
            )
        )

    # 4. Auto-generate AdversarialMutantSpecs (per branch + unread-tab block + OOD abstention)
    mutants: List[AdversarialMutantSpec] = []
    for branch in sop_spec.branches:
        mutants.append(
            AdversarialMutantSpec(
                mutant_id=f"mutant_{branch.branch_id}_happy",
                description=f"Verify induced branch {branch.branch_id} routes to {branch.disposition}",
                dossier=dict(branch.when),
                expected_disposition=branch.disposition,
                expected_blocked=False,
            )
        )
        if branch.required_read_artifacts:
            mutants.append(
                AdversarialMutantSpec(
                    mutant_id=f"mutant_{branch.branch_id}_unread_tab",
                    description=f"Block {branch.branch_id} when required tab {branch.required_read_artifacts[0]} is unread",
                    dossier=dict(branch.when),
                    expected_disposition=branch.disposition,
                    expected_blocked=True,
                    unread_artifacts=(branch.required_read_artifacts[0],),
                )
            )
    mutants.append(
        AdversarialMutantSpec(
            mutant_id="mutant_ood_abstain",
            description="Out-of-distribution feature vector must abstain to PREPARE",
            dossier={"ood_unseen_feature": "out_of_distribution"},
            expected_disposition="",
            expected_blocked=True,
        )
    )

    return AppCartridge(
        cartridge_id=cartridge_id,
        portal_origin=portal_origin,
        sop_spec=sop_spec,
        skills=tuple(skills),
        api_oracles=tuple(oracles),
        mutants=tuple(mutants),
        egress_audit=summarize_egress_ledger(guard.read_ledger()),
    )


def evaluate_cartridge_ood_support(
    cartridge: AppCartridge,
    observed_facts: Mapping[str, Any],
    *,
    min_branch_support: int = 1,
) -> OODSupportVerdict:
    """Stop silent extrapolation when a feature takes an unseen categorical value or branch support is too low."""
    domains = cartridge.sop_spec.observed_feature_domains
    unseen: Dict[str, str] = {}
    for k, val in observed_facts.items():
        if k in domains and domains[k]:
            norm_v = str(val).strip().casefold()
            if norm_v and norm_v not in domains[k]:
                unseen[str(k)] = str(val)
    if unseen:
        return OODSupportVerdict(
            in_support=False,
            unseen_features=unseen,
            requires_prepare=True,
            reason=f"unseen_categorical_feature_values:{sorted(unseen.keys())}",
        )

    matched_branch: Optional[SOPBranchSpec] = None
    for b in cartridge.sop_spec.branches:
        if all(
            _policy_value_matches(
                str(observed_facts.get(k, "")).strip().casefold(),
                str(v).strip().casefold(),
            )
            for k, v in b.when.items()
        ):
            matched_branch = b
            break
    if matched_branch is None:
        return OODSupportVerdict(
            in_support=False,
            unseen_features={},
            requires_prepare=True,
            reason="no_matching_sop_branch",
        )
    support = cartridge.sop_spec.branch_support_counts.get(matched_branch.branch_id, 0)
    if support < min_branch_support:
        return OODSupportVerdict(
            in_support=False,
            unseen_features={},
            requires_prepare=True,
            matched_branch_id=matched_branch.branch_id,
            branch_support=support,
            reason=f"insufficient_branch_support:{support}<{min_branch_support}",
        )
    return OODSupportVerdict(
        in_support=True,
        unseen_features={},
        requires_prepare=False,
        matched_branch_id=matched_branch.branch_id,
        branch_support=support,
        reason="in_support",
    )


def incorporate_prepare_resolution_into_cartridge(
    cartridge: AppCartridge,
    new_demo_trace: Mapping[str, Any],
    existing_traces: Sequence[Mapping[str, Any]],
    *,
    coding_agent_fn: Optional[Callable[[str], str]] = None,
    egress_policy: Optional[EgressPolicy] = None,
    ledger_path: Optional[Union[str, Path]] = None,
) -> Tuple[AppCartridge, CartridgeQualificationReport]:
    """Re-induce and offline-qualify an updated AppCartridge after a single human PrepareCard resolution."""
    updated = compile_demo_traces_to_cartridge(
        [*existing_traces, new_demo_trace],
        cartridge_id=cartridge.cartridge_id,
        portal_origin=cartridge.portal_origin,
        coding_agent_fn=coding_agent_fn,
        egress_policy=egress_policy,
        ledger_path=ledger_path,
    )
    qual = qualify_app_cartridge_offline(updated)
    return updated, qual


_RAW_NUM_RE = re.compile(r"^\s*[$€£¥]?\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)\s*%?\s*$")


def _parse_pure_number(raw: str) -> Optional[float]:
    s = str(raw or "").strip()
    if not s:
        return None
    cents = parse_canonical_currency_cents(s)
    if cents is not None:
        return cents / 100.0
    m = _RAW_NUM_RE.match(s)
    return float(m.group(1).replace(",", "")) if m else None


def evaluate_defeasible_branch(
    branch: SOPBranchSpec,
    case_evidence: Union[Mapping[str, Any], str],
    *,
    defeater_detector_fn: Optional[
        Callable[[SOPBranchSpec, Union[Mapping[str, Any], str]], Optional[str]]
    ] = None,
) -> DefeasibleVerdict:
    """Block a matched SOP branch when an undercutting defeater is present in case evidence."""
    if defeater_detector_fn is not None:
        detected = defeater_detector_fn(branch, case_evidence)
        if detected:
            return DefeasibleVerdict(False, True, str(detected), True, branch.branch_id)
    if isinstance(case_evidence, Mapping):
        ev_cf = " ".join(f"{k} {v}" for k, v in case_evidence.items() if v is not None).casefold()
        falsy = {"", "false", "no", "none", "0"}
        for d in branch.unless_defeaters:
            dc = d.strip()
            if not dc:
                continue
            d_cf, d_sp = dc.casefold(), dc.replace("_", " ").casefold()
            key_hit = any(
                str(k).strip().casefold() in (d_cf, d_sp)
                and bool(v)
                and str(v).strip().casefold() not in falsy
                for k, v in case_evidence.items()
            )
            if key_hit or d_cf in ev_cf or d_sp in ev_cf:
                return DefeasibleVerdict(False, True, f"defeater_present:{dc}", True, branch.branch_id)
    else:
        ev_cf = str(case_evidence or "").casefold()
        for d in branch.unless_defeaters:
            dc = d.strip()
            if dc and (dc.casefold() in ev_cf or dc.replace("_", " ").casefold() in ev_cf):
                return DefeasibleVerdict(False, True, f"defeater_present:{dc}", True, branch.branch_id)
    return DefeasibleVerdict(True, False, "", False, branch.branch_id)


def _anchored_note_slots(demos: Sequence[RawDemoTrace], note_field: str) -> Tuple[str, ...]:
    """Discover $slot note keys anchored via SpanAnchor directly to dom_observations."""
    per_demo_slots: List[List[str]] = []
    for d in demos:
        note = str(d.committed_fields.get(note_field, "") or "")
        if not note:
            per_demo_slots.append([])
            continue
        note_cf = note.casefold()
        found: List[str] = []
        for obs_k, obs_v in d.dom_observations.items():
            raw_obs = str(obs_v or "")
            if not raw_obs.strip():
                continue
            cand = raw_obs.strip()
            pos = raw_obs.find(cand)
            if len(cand) >= 2 and cand.casefold() in note_cf and pos >= 0:
                if verify_span_anchor(d.dom_observations, SpanAnchor(obs_k, pos, pos + len(cand), cand)):
                    found.append(obs_k)
                    continue
            for m in re.finditer(r"[A-Za-z0-9][A-Za-z0-9_./-]{2,}", raw_obs):
                tok = m.group(0)
                if tok.casefold() in note_cf and verify_span_anchor(
                    d.dom_observations, SpanAnchor(obs_k, m.start(), m.end(), tok)
                ):
                    found.append(obs_k)
                    break
        per_demo_slots.append(found)
    if not per_demo_slots:
        return ()
    common = set(per_demo_slots[0])
    for s in per_demo_slots[1:]:
        common &= set(s)
    return tuple(k for k in per_demo_slots[0] if k in common)


def _induce_contrastive_when(
    disp: str,
    disp_demos: Sequence[RawDemoTrace],
    other_demos: Sequence[RawDemoTrace],
) -> Dict[str, Any]:
    all_keys = sorted({k for d in disp_demos for k in d.dom_observations.keys()})
    when: Dict[str, Any] = {}
    for k in all_keys:
        d_vals = [str(d.dom_observations.get(k, "")).strip() for d in disp_demos]
        if not all(d_vals):
            continue
        o_vals = [str(d.dom_observations.get(k, "")).strip() for d in other_demos if k in d.dom_observations]
        d_cf = {v.casefold() for v in d_vals}
        o_cf = {v.casefold() for v in o_vals}
        if len(d_cf) == 1 and (not other_demos or not (d_cf & o_cf)):
            when[k] = d_vals[0]
            continue
        d_nums = [_parse_pure_number(v) for v in d_vals]
        o_nums = [_parse_pure_number(v) for v in o_vals]
        if other_demos and all(n is not None for n in d_nums) and o_nums and all(n is not None for n in o_nums):
            d_f = [float(n) for n in d_nums if n is not None]
            o_f = [float(n) for n in o_nums if n is not None]
            if min(d_f) > max(o_f):
                when[k] = f">= {min(d_f):g}"
            elif max(d_f) < min(o_f):
                when[k] = f"<= {max(d_f):g}"
            continue
        if other_demos:
            d_tok_sets = [set(re.findall(r"[^\W_]{3,}", v.casefold())) for v in d_vals]
            o_toks = {t for v in o_vals for t in re.findall(r"[^\W_]{3,}", v.casefold())}
            shared_unique = sorted((set.intersection(*d_tok_sets) if d_tok_sets else set()) - o_toks)
            if shared_unique:
                when[k] = shared_unique[0]
    if not when and disp_demos:
        for k, v in disp_demos[0].dom_observations.items():
            if str(v).strip():
                when[k] = str(v).strip()
                break
    return when


def induce_cartridge_from_raw_demos(
    raw_demos: Sequence[RawDemoTrace],
    *,
    cartridge_id: str,
    portal_origin: str,
    disposition_field: str = "disposition",
    note_field: str = "notes",
    coding_agent_fn: Optional[Callable[[str], str]] = None,
    egress_policy: Optional[EgressPolicy] = None,
    ledger_path: Optional[Union[str, Path]] = None,
) -> AppCartridge:
    """Induce an AppCartridge directly from raw unlabelled demonstrations with zero pre-structured feature dicts."""
    if not raw_demos:
        raise ValueError("induce_cartridge_from_raw_demos requires at least one RawDemoTrace")

    guard = _make_guard(egress_policy, ledger_path, "oa_raw_demo_egress_")
    by_disp: Dict[str, List[RawDemoTrace]] = {}
    for d in raw_demos:
        disp = str(d.committed_fields.get(disposition_field, "")).strip() or "default"
        by_disp.setdefault(disp, []).append(d)

    read_views_by_disp: Dict[str, Tuple[str, ...]] = {}
    post_commit_by_disp: Dict[str, Tuple[str, ...]] = {}
    note_slots_by_disp: Dict[str, Tuple[str, ...]] = {}
    for disp, demos in by_disp.items():
        common_views = set(demos[0].visited_views)
        for d in demos[1:]:
            common_views &= set(d.visited_views)
        read_views_by_disp[disp] = tuple(v for v in demos[0].visited_views if v in common_views)

        common_post = set(demos[0].post_commit_actions)
        for d in demos[1:]:
            common_post &= set(d.post_commit_actions)
        post_tuple = tuple(a for a in demos[0].post_commit_actions if a in common_post)
        if post_tuple:
            post_commit_by_disp[disp] = post_tuple
        note_slots_by_disp[disp] = _anchored_note_slots(demos, note_field)

    branches: List[SOPBranchSpec] = []
    if coding_agent_fn is not None:
        codec = BijectiveSymbolCodec()
        ord_codec = OrderPreservingRankCodec().fit(
            v for d in raw_demos for v in (*d.dom_observations.values(), *d.committed_fields.values())
        )

        def _sym(text: str) -> str:
            return codec.symbolize_text(re.sub(r"\$(?=\d)", " ", text or ""))

        def _sym_val(text: str) -> str:
            ord_tok = ord_codec.encode_value(text)
            return ord_tok if ord_tok is not None else _sym(text)

        outbound = {
            "demo_traces": [
                {
                    "summary": _sym(d.goal_text),
                    "visited_tabs": [_sym(v) for v in d.visited_views],
                    "observations": {_sym(k): _sym_val(v) for k, v in d.dom_observations.items()},
                    "fields": {_sym(k): _sym_val(v) for k, v in d.committed_fields.items()},
                    "actions": [_sym(a) for a in d.post_commit_actions],
                }
                for d in raw_demos
            ]
        }
        verdict = guard.check_and_record(outbound, destination="raw_demo_inducer")
        if not verdict.allowed:
            raise EgressBlockedError(verdict)
        raw_reply = coding_agent_fn(
            'Induce JSON {"branches": [{"branch_id", "when", "disposition", "required_read_artifacts", '
            '"required_note_slots", "priority"}]} from symbolized demos:\n'
            + json.dumps(outbound, sort_keys=True)
            + "\n"
        )
        parsed = _extract_json_dict(codec.detokenize_text(ord_codec.detokenize_text(raw_reply)))
        for b in _parse_branch_dicts(parsed.get("branches") or []):
            reads = b.required_read_artifacts or read_views_by_disp.get(b.disposition, ())
            slots = b.required_note_slots or note_slots_by_disp.get(b.disposition, ())
            branches.append(
                SOPBranchSpec(b.branch_id, b.when, b.disposition, reads, slots, b.priority, b.unless_defeaters)
            )

    if not branches:
        for idx, (disp, demos) in enumerate(by_disp.items(), start=1):
            others = [d for od, od_list in by_disp.items() if od != disp for d in od_list]
            when = _induce_contrastive_when(disp, demos, others)
            branches.append(
                SOPBranchSpec(
                    branch_id=f"raw_branch_{idx}",
                    when=when,
                    disposition=disp,
                    required_read_artifacts=read_views_by_disp[disp],
                    required_note_slots=note_slots_by_disp[disp],
                    priority=idx,
                )
            )

    base_sop = _build_compiled_sop(f"{cartridge_id}_sop", branches)
    domains: Dict[str, set[str]] = {}
    for d in raw_demos:
        for k, v in d.dom_observations.items():
            sv = str(v).strip()
            if sv and _parse_pure_number(sv) is None:
                domains.setdefault(k, set()).add(sv.casefold())
    support_counts = {b.branch_id: len(by_disp.get(b.disposition, ())) for b in base_sop.branches}
    sop_spec = CompiledSOPSpec(
        spec_id=base_sop.spec_id,
        decision_table=base_sop.decision_table,
        branches=base_sop.branches,
        consistency_ok=base_sop.consistency_ok,
        consistency_conflicts=base_sop.consistency_conflicts,
        observed_feature_domains={k: tuple(sorted(vs)) for k, vs in domains.items()},
        branch_support_counts=support_counts,
        post_commit_obligations=post_commit_by_disp,
    )

    skills: List[SkillMacroSpec] = []
    for disp, demos in by_disp.items():
        slug = re.sub(r"[^a-z0-9]+", "_", disp.lower()).strip("_") or "action"
        slots = note_slots_by_disp.get(disp, ())
        skills.append(
            SkillMacroSpec(
                skill_id=f"skill_{slug}",
                name=f"execute_{slug}",
                description=f"Induced skill for {disp}",
                parameters=slots,
                code="def repair(ui, vars):\n    ui.click(0)\n",
                expected_url_pattern=demos[0].post_commit_url,
            )
        )

    oracles: List[APIOracleSpec] = []
    seen_urls: set[str] = set()
    for d in raw_demos:
        if d.post_commit_url and d.post_commit_url not in seen_urls:
            seen_urls.add(d.post_commit_url)
            oracles.append(
                APIOracleSpec(
                    oracle_id=f"oracle_{len(oracles) + 1}",
                    method="GET",
                    path_pattern=d.post_commit_url,
                    expected_fields=tuple(sorted(d.committed_fields.keys())),
                    read_only=True,
                )
            )

    mutants: List[AdversarialMutantSpec] = []
    for b in sop_spec.branches:
        rep_demo = (by_disp.get(b.disposition) or list(raw_demos))[0]
        dossier = dict(rep_demo.dom_observations)
        mutants.append(
            AdversarialMutantSpec(
                mutant_id=f"mutant_{b.branch_id}_happy",
                description=f"Verify {b.branch_id} routes to {b.disposition}",
                dossier=dossier,
                expected_disposition=b.disposition,
                expected_blocked=False,
            )
        )
        if b.required_read_artifacts:
            mutants.append(
                AdversarialMutantSpec(
                    mutant_id=f"mutant_{b.branch_id}_unread",
                    description=f"Block {b.branch_id} when {b.required_read_artifacts[0]} is unread",
                    dossier=dossier,
                    expected_disposition=b.disposition,
                    expected_blocked=True,
                    unread_artifacts=(b.required_read_artifacts[0],),
                )
            )
    mutants.append(
        AdversarialMutantSpec(
            mutant_id="mutant_ood_abstain",
            description="Unseen OOD observation must abstain",
            dossier={"ood_unseen_feature": "out_of_distribution"},
            expected_disposition="",
            expected_blocked=True,
        )
    )

    return AppCartridge(
        cartridge_id=cartridge_id,
        portal_origin=portal_origin,
        sop_spec=sop_spec,
        skills=tuple(skills),
        api_oracles=tuple(oracles),
        mutants=tuple(mutants),
        egress_audit=summarize_egress_ledger(guard.read_ledger()),
    )
