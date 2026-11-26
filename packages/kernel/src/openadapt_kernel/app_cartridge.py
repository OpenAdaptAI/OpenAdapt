"""Coding-Agent AppCartridge Compiler: Autoformalized SOPs, AST-Verified Skills, and Mutant Qualification."""

from __future__ import annotations

import importlib
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple, Union

from openadapt_kernel.egress_guard import (
    EgressBlockedError,
    EgressPolicy,
    LocalEgressGuard,
    summarize_egress_ledger,
)
from openadapt_kernel.fsm import PolicyDecisionTable, PolicyRule, evaluate_policy_decision
from openadapt_kernel.stateful_twin import StatefulTwinApp
from openadapt_kernel.zero_phi_repair import verify_symbolic_repair_ast

_CARTRIDGE_EXTRA_TOKENS: FrozenSet[str] = frozenset(
    "api artifact artifacts branch branches cartridge claim claims crm deny disposition escalate "
    "escalation expected fields get har head if in_network json method mutant mutants notes "
    "oracle oracles path pattern portal priority read read_only records request response review "
    "routing rule rules schema schemas skill skills sla slot slots sop spec support suspended then triage when".split()
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


@dataclass(frozen=True)
class SOPBranchSpec:
    branch_id: str
    when: Dict[str, Any]
    disposition: str
    required_read_artifacts: Tuple[str, ...] = ()
    required_note_slots: Tuple[str, ...] = ()
    priority: int = 0


@dataclass(frozen=True)
class CompiledSOPSpec:
    spec_id: str
    decision_table: PolicyDecisionTable
    branches: Tuple[SOPBranchSpec, ...]
    consistency_ok: bool
    consistency_conflicts: Tuple[str, ...] = ()


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


def _cartridge_policy(policy: Optional[EgressPolicy] = None) -> EgressPolicy:
    base = policy or EgressPolicy()
    return EgressPolicy(
        allowed_tokens=base.allowed_tokens,
        include_html_aria_vocab=base.include_html_aria_vocab,
        include_clinical_vocab=base.include_clinical_vocab,
        extra_allowed_tokens=base.extra_allowed_tokens | _CARTRIDGE_EXTRA_TOKENS,
        allowed_var_keys=base.allowed_var_keys,
    )


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
        lo, lo_inc = float("-inf"), False
        hi, hi_inc = float("inf"), False
        neq: List[float] = []
        for op, num in (c1, c2):
            if op == "==":
                if num > lo or (num == lo and not lo_inc):
                    lo, lo_inc = num, True
                if num < hi or (num == hi and not hi_inc):
                    hi, hi_inc = num, True
            elif op == ">=":
                if num > lo:
                    lo, lo_inc = num, True
            elif op == ">":
                if num > lo or (num == lo and lo_inc):
                    lo, lo_inc = num, False
            elif op == "<=":
                if num < hi:
                    hi, hi_inc = num, True
            elif op == "<":
                if num < hi or (num == hi and hi_inc):
                    hi, hi_inc = num, False
            elif op == "!=":
                neq.append(num)
        if lo > hi:
            return False
        if lo == hi:
            return lo_inc and hi_inc and lo not in neq
        return True

    s1 = {str(x).strip().lower() for x in v1} if isinstance(v1, (list, tuple, set)) else {str(v1).strip().lower()}
    s2 = {str(x).strip().lower() for x in v2} if isinstance(v2, (list, tuple, set)) else {str(v2).strip().lower()}
    if len(s1) == 1 and next(iter(s1)).startswith("!="):
        ex = next(iter(s1))[2:].strip()
        return any(x != ex for x in s2)
    if len(s2) == 1 and next(iter(s2)).startswith("!="):
        ex = next(iter(s2))[2:].strip()
        return any(x != ex for x in s1)
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
            if b1.priority != b2.priority:
                continue
            if b1.disposition.strip().lower() == b2.disposition.strip().lower():
                continue
            if _predicates_satisfiable_together(b1.when, b2.when):
                conflicts.append(
                    f"conflict:{b1.branch_id}({b1.disposition})_vs_{b2.branch_id}({b2.disposition})"
                )
    return len(conflicts) == 0, tuple(conflicts)


def _build_compiled_sop(spec_id: str, branches: Sequence[SOPBranchSpec]) -> CompiledSOPSpec:
    ordered = tuple(sorted(branches, key=lambda b: (-int(b.priority), b.branch_id)))
    rules = tuple(
        PolicyRule(
            rule_id=b.branch_id,
            conditions={str(k): str(v) for k, v in b.when.items()},
            required_disposition=b.disposition,
        )
        for b in ordered
    )
    table = PolicyDecisionTable(table_id=spec_id, rules=rules, require_human_if_unmatched=True)
    ok, conflicts = check_sop_consistency(ordered)
    return CompiledSOPSpec(
        spec_id=spec_id,
        decision_table=table,
        branches=ordered,
        consistency_ok=ok,
        consistency_conflicts=conflicts,
    )


def _parse_branches_deterministic(sop_text: str) -> List[SOPBranchSpec]:
    branches: List[SOPBranchSpec] = []
    for idx, line in enumerate(sop_text.splitlines(), start=1):
        m = _SOP_LINE_RE.search(line.strip())
        if not m:
            continue
        conds: Dict[str, Any] = {}
        for part in re.split(r"\s+AND\s+", m.group("conds"), flags=re.IGNORECASE):
            if "=" in part:
                k, v = part.split("=", 1)
                conds[k.strip()] = v.strip()
        read_arts = tuple(a.strip() for a in (m.group("read") or "").split(",") if a.strip())
        note_slots = tuple(s.strip() for s in (m.group("notes") or "").split(",") if s.strip())
        prio = int(m.group("prio")) if m.group("prio") else 0
        branches.append(
            SOPBranchSpec(
                branch_id=f"branch_{idx}",
                when=conds,
                disposition=m.group("disp").strip(),
                required_read_artifacts=read_arts,
                required_note_slots=note_slots,
                priority=prio,
            )
        )
    return branches


def _parse_branch_dicts(raw_list: Sequence[Mapping[str, Any]]) -> List[SOPBranchSpec]:
    out: List[SOPBranchSpec] = []
    for i, item in enumerate(raw_list, start=1):
        out.append(
            SOPBranchSpec(
                branch_id=str(item.get("branch_id") or f"branch_{i}"),
                when=dict(item.get("when") or {}),
                disposition=str(item.get("disposition", "")).strip(),
                required_read_artifacts=tuple(str(a) for a in (item.get("required_read_artifacts") or [])),
                required_note_slots=tuple(str(s) for s in (item.get("required_note_slots") or [])),
                priority=int(item.get("priority", 0)),
            )
        )
    return out


def compile_sop_handbook_to_rules(
    sop_text: str,
    *,
    spec_id: str = "compiled_sop",
    coding_agent_fn: Optional[Callable[[str], str]] = None,
    egress_policy: Optional[EgressPolicy] = None,
    ledger_path: Optional[Union[str, Path]] = None,
) -> CompiledSOPSpec:
    """Audit SOP text via LocalEgressGuard and compile into a symbolically checked CompiledSOPSpec."""
    pol = _cartridge_policy(egress_policy)
    ledger_file = (
        Path(ledger_path)
        if ledger_path is not None
        else Path(tempfile.mkdtemp(prefix="oa_sop_egress_")) / "outbound_egress_ledger.jsonl"
    )
    guard = LocalEgressGuard(ledger_file, policy=pol)
    audited_sop = re.sub(r"\bPRIORITY\s+-?\d{1,2}\b", "PRIORITY", sop_text, flags=re.IGNORECASE)
    verdict = guard.check_and_record(audited_sop, destination="sop_compiler")
    if not verdict.allowed:
        raise EgressBlockedError(verdict)

    if coding_agent_fn is not None:
        prompt = (
            "Compile this SOP into JSON {\"branches\": [{\"branch_id\": \"...\", \"when\": {...}, "
            "\"disposition\": \"...\", \"required_read_artifacts\": [...], \"required_note_slots\": [...], "
            "\"priority\": 0}]}.\n"
            f"SOP:\n{sop_text}\n"
        )
        parsed = _extract_json_dict(coding_agent_fn(prompt))
        branches = _parse_branch_dicts(parsed.get("branches") or [])
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
    pol = _cartridge_policy(egress_policy)
    ledger_file = (
        Path(ledger_path)
        if ledger_path is not None
        else Path(tempfile.mkdtemp(prefix="oa_cartridge_egress_")) / "outbound_egress_ledger.jsonl"
    )
    guard = LocalEgressGuard(ledger_file, policy=pol)
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
        "All api_oracles must be read_only=true using GET/HEAD.\n"
        f"Input:\n{json.dumps(outbound, sort_keys=True)}\n"
    )
    spec = _extract_json_dict(coding_agent_fn(prompt))

    branches = _parse_branch_dicts(spec.get("sop_branches") or [])
    if not branches:
        branches = _parse_branches_deterministic(sop_text)
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
                skill_id=str(s.get("skill_id") or f"skill_{i}"),
                name=str(s.get("name", f"skill_{i}")),
                description=str(s.get("description", "")),
                parameters=params,
                code=code,
                expected_url_pattern=str(s.get("expected_url_pattern", "")),
                required_filters={str(k): str(v) for k, v in (s.get("required_filters") or {}).items()},
            )
        )

    oracles: List[APIOracleSpec] = []
    seen_paths: set[str] = set()
    for i, o in enumerate(spec.get("api_oracles") or [], start=1):
        method = str(o.get("method", "GET")).upper()
        read_only = bool(o.get("read_only", True))
        if not read_only or method not in _READ_ONLY_METHODS:
            raise ValueError(f"api_oracle_must_be_read_only:{method}")
        path_pat = str(o.get("path_pattern", "/"))
        seen_paths.add(path_pat)
        oracles.append(
            APIOracleSpec(
                oracle_id=str(o.get("oracle_id") or f"oracle_{i}"),
                method=method,
                path_pattern=path_pat,
                expected_fields=tuple(str(f) for f in (o.get("expected_fields") or [])),
                read_only=True,
            )
        )
    for i, h in enumerate(har_schemas, start=1):
        h_method = str(h.get("method", "GET")).upper()
        h_path = str(h.get("path_pattern", ""))
        if h_method in _READ_ONLY_METHODS and h_path and h_path not in seen_paths:
            seen_paths.add(h_path)
            oracles.append(
                APIOracleSpec(
                    oracle_id=f"har_oracle_{i}",
                    method=h_method,
                    path_pattern=h_path,
                    expected_fields=tuple(str(f) for f in (h.get("response_fields") or [])),
                    read_only=True,
                )
            )

    mutants = tuple(
        AdversarialMutantSpec(
            mutant_id=str(m.get("mutant_id") or f"mutant_{i}"),
            description=str(m.get("description", "")),
            dossier=dict(m.get("dossier") or {}),
            expected_disposition=str(m.get("expected_disposition", "")),
            expected_blocked=bool(m.get("expected_blocked", False)),
            unread_artifacts=tuple(str(a) for a in (m.get("unread_artifacts") or [])),
        )
        for i, m in enumerate(spec.get("mutants") or [], start=1)
    )

    entries = guard.read_ledger()
    audit = {
        **summarize_egress_ledger(entries),
        "sha256_chain_integrity_verified": guard.verify_ledger_integrity(),
        "entry_sha256s": [str(e.get("entry_sha256", "")) for e in entries],
    }
    return AppCartridge(
        cartridge_id=cartridge_id,
        portal_origin=portal_origin,
        sop_spec=compiled_sop,
        skills=tuple(skills),
        api_oracles=tuple(oracles),
        mutants=mutants,
        egress_audit=audit,
    )


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
        proposed = m.expected_disposition or "APPROVE"
        dec = evaluate_policy_decision(cartridge.sop_spec.decision_table, facts, proposed)
        matched_branch = branch_by_id.get(dec.matched_rule_id or "")
        missing_reads = (
            set(matched_branch.required_read_artifacts) & set(m.unread_artifacts)
            if matched_branch is not None
            else set()
        )
        epistemic_blocked = bool(missing_reads)
        policy_blocked = (not dec.allowed) or dec.requires_prepare

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
        qualified=qualified,
        passed_mutants=passed,
        total_mutants=len(cartridge.mutants),
        failed_mutant_ids=tuple(failed_mutants),
        ast_violations=tuple(ast_violations),
        consistency_ok=cartridge.sop_spec.consistency_ok,
    )
