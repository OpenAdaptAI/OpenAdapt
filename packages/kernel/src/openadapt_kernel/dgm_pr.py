"""Outer-Loop Offline Self-Improving Kernel PR Harness (DGM-PR)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple, Union

from openadapt_kernel.egress_guard import (
    EgressBlockedError,
    EgressPolicy,
    LocalEgressGuard,
    inspect_outbound_payload,
)
from openadapt_kernel.zero_phi_repair import sanitize_ui_label_allowlist

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")

# SHA-256 digests of lowercased forbidden vendor/app identifiers so zero literal app names appear in kernel source.
_FORBIDDEN_APP_HASHES: FrozenSet[str] = frozenset(
    {
        "2cbff87311a9592278e5a0d89533665b0aa743867f81e75102515882558cad3b",
        "7e2e312994fb64698dfcabe50052cc29d98445e713a03ea2ffb7f2eec4310c32",
        "22f58b5b281d5633435bbae666174ce4b9902ea8df082a2bc3d4d04754b25549",
        "45e7090c069607c44a195bcf9759372e15474907f5f25864f3f7b3e8d0622440",
        "8a9937ead06ccf815cc59205ff1cc69bfe87a421bef20e446b93f494418048b4",
        "421d09fdf4c226c5fe2c190d9481bde84755da1d65f500a49f655210f0545464",
        "4f4333e4f800b91ab8e8b4bdaf4c613865a3f465706c8b28496074c4082fd9dd",
        "b6542d155d54974e48af8eff3398b36f47ded8c9455493952981fbdbe1ba3594",
        "a4251bfb96fdb9419aa8c7340cd6a4be742d35ea4ed7fd996fa9f7e6771e8051",
    }
)

_FORBIDDEN_LEGACY_IMPORT_RE = re.compile(
    r"^\+\s*(?:from|import)\s+(openadapt_flow|openadapt_desktop|openadapt_capture)\b"
)

_VERIFIER_SAFETY_FILES: FrozenSet[str] = frozenset(
    {
        "egress_guard.py",
        "fsm.py",
        "interceptor.py",
        "ledger.py",
        "oracles.py",
        "prepare.py",
        "zero_phi_repair.py",
    }
)

_WEAKENED_CHECK_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"^-\s*assert\b"),
    re.compile(r"^-\s*raise\s+(?:EgressBlockedError|ProvenanceViolation|RubberStampViolationError|PermissionError)\b"),
    re.compile(r"^-\s*if\s+not\s+(?:verdict\.allowed|ast_check\.ok|proof\.safe)\b"),
    re.compile(r"^-\s*return\s+EgressVerdict\(\s*allowed\s*=\s*False"),
    re.compile(r"^-\s*(?:verdict|ast_check)\s*=\s*(?:inspect_outbound_payload|verify_symbolic_repair_ast)\("),
    re.compile(r"^-\s*verify_fsm_safety\("),
)

_BYPASS_ADDITION_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"^\+\s*return\s+True\s*#\s*(?:skip|bypass|disable|temporary)", re.IGNORECASE),
    re.compile(r"^\+\s*requires_human_merge\s*[:=].*False"),
)


@dataclass(frozen=True)
class FailureRecord:
    """Single observed failure event from a PortalPack run (may contain raw tenant/PHI fields)."""

    pack_id: str
    failure_category: str
    signature_tokens: Tuple[str, ...] = ()
    error_message: str = ""
    tenant_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FailureCluster:
    """Anonymized cluster of recurring kernel failure signatures across PortalPacks."""

    cluster_id: str
    failure_category: str
    normalized_signature: str
    pack_ids: Tuple[str, ...]
    occurrence_count: int
    sample_tokens: Tuple[str, ...]


@dataclass(frozen=True)
class KernelPRProposal:
    """Auditable, human-gated kernel improvement PR proposal (never auto-merged)."""

    proposal_id: str
    cluster_id: str
    title: str
    summary: str
    patch_diff: str
    changed_files: Tuple[str, ...]
    verification_summary: Dict[str, Any]
    safety_ok: bool
    safety_violations: Tuple[str, ...] = ()
    requires_human_merge: bool = True
    auto_merge_allowed: bool = False

    def __post_init__(self) -> None:
        if self.requires_human_merge is not True:
            raise ValueError("KernelPRProposal.requires_human_merge must always be True")
        if self.auto_merge_allowed is not False:
            raise ValueError("KernelPRProposal.auto_merge_allowed must always be False")


def _anonymize_pack_id(pack_id: str) -> str:
    digest = hashlib.sha256(pack_id.encode("utf-8")).hexdigest()[:12]
    return f"pack_{digest}"


def _sanitize_tokens(tokens: Sequence[str], error_message: str, policy: EgressPolicy) -> Tuple[str, ...]:
    vocab = policy.effective_tokens()
    cleaned: List[str] = []
    seen: set[str] = set()
    for raw in tokens:
        for tok in _WORD_RE.findall(raw or ""):
            low = tok.lower()
            if low in vocab and low not in seen:
                seen.add(low)
                cleaned.append(low)
    for tok in _WORD_RE.findall(sanitize_ui_label_allowlist(error_message)):
        low = tok.lower()
        if low in vocab and low not in seen:
            seen.add(low)
            cleaned.append(low)
    return tuple(sorted(cleaned)[:16])


def cluster_pack_failures(
    records: Sequence[FailureRecord],
    *,
    min_cluster_size: int = 2,
    policy: Optional[EgressPolicy] = None,
) -> List[FailureCluster]:
    """Group anonymized recurring failure signatures across PortalPacks while stripping tenant/PHI data."""
    pol = policy or EgressPolicy()
    grouped: Dict[Tuple[str, str], List[Tuple[str, Tuple[str, ...]]]] = {}

    for rec in records:
        category = sanitize_ui_label_allowlist(rec.failure_category.replace("_", " ")).replace(" ", "_") or "unknown"
        tokens = _sanitize_tokens(rec.signature_tokens, rec.error_message, pol)
        norm_sig = " ".join(tokens) if tokens else category
        anon_pack = _anonymize_pack_id(rec.pack_id)
        grouped.setdefault((category, norm_sig), []).append((anon_pack, tokens))

    clusters: List[FailureCluster] = []
    threshold = max(1, int(min_cluster_size))
    for (category, norm_sig), items in grouped.items():
        if len(items) < threshold:
            continue
        pack_ids = tuple(sorted({p for p, _ in items}))
        sample_toks = tuple(sorted({t for _, toks in items for t in toks})[:16])
        cid = hashlib.sha256(f"{category}:{norm_sig}".encode("utf-8")).hexdigest()[:16]
        clusters.append(
            FailureCluster(
                cluster_id=cid,
                failure_category=category,
                normalized_signature=norm_sig,
                pack_ids=pack_ids,
                occurrence_count=len(items),
                sample_tokens=sample_toks,
            )
        )

    clusters.sort(key=lambda c: (-c.occurrence_count, c.cluster_id))
    return clusters


def _has_regression_test(changed_files: Sequence[str]) -> bool:
    for path in changed_files:
        norm = path.replace("\\", "/")
        parts = norm.split("/")
        fname = parts[-1] if parts else ""
        if "tests" in parts and fname.startswith("test_") and fname.endswith(".py"):
            return True
    return False


def _find_forbidden_app_tokens(text: str, extra_forbidden_names: Sequence[str]) -> List[str]:
    extra_hashes = {
        hashlib.sha256(name.strip().lower().encode("utf-8")).hexdigest()
        for name in extra_forbidden_names
        if name.strip()
    }
    target_hashes = _FORBIDDEN_APP_HASHES | extra_hashes
    hits: List[str] = []
    seen: set[str] = set()
    for tok in _WORD_RE.findall(text):
        low = tok.lower()
        digest = hashlib.sha256(low.encode("utf-8")).hexdigest()
        if digest in target_hashes and low not in seen:
            seen.add(low)
            hits.append(low)
    return hits


def validate_kernel_pr_safety(
    diff_text: str,
    *,
    changed_files: Sequence[str],
    extra_forbidden_names: Sequence[str] = (),
) -> Tuple[bool, List[str]]:
    """Enforce hard safety invariants on any candidate outer-loop kernel PR diff."""
    violations: List[str] = []

    if not diff_text or not diff_text.strip():
        violations.append("empty_patch_diff")
        return False, violations

    if not _has_regression_test(changed_files):
        violations.append("missing_regression_test")

    all_text = diff_text + "\n" + "\n".join(changed_files)
    forbidden_hits = _find_forbidden_app_tokens(all_text, extra_forbidden_names)
    for hit in forbidden_hits:
        violations.append(f"forbidden_app_name:{hit}")

    current_file = ""
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            continue
        if line.startswith("--- a/"):
            if not current_file:
                current_file = line[6:].strip()
            continue

        m_imp = _FORBIDDEN_LEGACY_IMPORT_RE.match(line)
        if m_imp is not None:
            violations.append(f"forbidden_legacy_import:{m_imp.group(1)}")

        for bypass_pat in _BYPASS_ADDITION_PATTERNS:
            if bypass_pat.search(line):
                violations.append("weakened_verifier_or_egress_check:bypass_added")

        if line.startswith("-") and not line.startswith("---"):
            fname = current_file.rsplit("/", 1)[-1] if current_file else ""
            in_safety_file = fname in _VERIFIER_SAFETY_FILES or any(
                f.rsplit("/", 1)[-1] in _VERIFIER_SAFETY_FILES for f in changed_files
            )
            for weak_pat in _WEAKENED_CHECK_PATTERNS:
                if weak_pat.search(line):
                    if in_safety_file or "egress" in line.lower() or "verify_" in line.lower() or "assert " in line:
                        violations.append("weakened_verifier_or_egress_check")
                        break

    deduped = list(dict.fromkeys(violations))
    return len(deduped) == 0, deduped


def draft_kernel_pr_proposal(
    cluster: FailureCluster,
    *,
    patch_diff: str,
    changed_files: Sequence[str],
    verification_summary: Dict[str, Any],
    extra_forbidden_names: Sequence[str] = (),
) -> KernelPRProposal:
    """Draft an auditable, human-gated KernelPRProposal from a recurring failure cluster."""
    safety_ok, violations = validate_kernel_pr_safety(
        patch_diff,
        changed_files=changed_files,
        extra_forbidden_names=extra_forbidden_names,
    )
    prop_digest = hashlib.sha256(f"{cluster.cluster_id}\n{patch_diff}".encode("utf-8")).hexdigest()[:16]
    title = f"fix(kernel): resolve recurring {cluster.failure_category} across {len(cluster.pack_ids)} packs"
    summary = (
        f"Cluster {cluster.cluster_id} ({cluster.occurrence_count} occurrences across "
        f"{len(cluster.pack_ids)} anonymized packs): {cluster.normalized_signature}. "
        f"Safety validation: {'passed' if safety_ok else 'blocked'}."
    )
    return KernelPRProposal(
        proposal_id=f"dgm_pr_{prop_digest}",
        cluster_id=cluster.cluster_id,
        title=title,
        summary=summary,
        patch_diff=patch_diff,
        changed_files=tuple(changed_files),
        verification_summary=dict(verification_summary),
        safety_ok=safety_ok,
        safety_violations=tuple(violations),
        requires_human_merge=True,
        auto_merge_allowed=False,
    )


_API_KEY_ENV_VARS: Tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)

_DGM_EXTRA_TOKENS: FrozenSet[str] = frozenset(
    "cluster cluster_id failure failure_category json normalized_signature occurrence_count "
    "pack pack_ids patch proposal regression sample_tokens signature unified diff".split()
)


def _extract_json_block(raw_text: str) -> Optional[Dict[str, Any]]:
    text = (raw_text or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def make_cli_coding_agent(
    binary: str = "claude",
    *,
    extra_args: Sequence[str] = ("-p", "--output-format", "json", "--tools", ""),
    command_template: Optional[Sequence[str]] = None,
    timeout_s: float = 120.0,
    egress_guard: Optional[LocalEgressGuard] = None,
    egress_policy: Optional[EgressPolicy] = None,
    strip_api_key_env: bool = True,
) -> Callable[[str], str]:
    """Build a pluggable CLI coding-agent callable (claude, codex, gemini, etc.) with pre-spawn egress gating."""
    cmd = list(command_template) if command_template else [binary, *extra_args]
    dest = f"cli:{Path(cmd[0]).name}"

    def _invoke(prompt: str) -> str:
        if egress_guard is not None:
            verdict = egress_guard.check_and_record(prompt, destination=dest)
            if not verdict.allowed:
                raise EgressBlockedError(verdict)
        elif egress_policy is not None:
            verdict = inspect_outbound_payload(prompt, egress_policy)
            if not verdict.allowed:
                raise EgressBlockedError(verdict)
        else:
            # Always block raw images/base64 blobs even when no custom policy is supplied.
            bin_check = inspect_outbound_payload(prompt, EgressPolicy())
            if bin_check.reason == "binary_or_image_blocked":
                raise EgressBlockedError(bin_check)

        env = dict(os.environ)
        if strip_api_key_env:
            for key in _API_KEY_ENV_VARS:
                env.pop(key, None)

        proc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=float(timeout_s),
            env=env,
            check=False,
        )
        if proc.returncode != 0:
            err_tail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise RuntimeError(f"coding_agent_cli_failed:{proc.returncode}:{err_tail}")

        out = (proc.stdout or "").strip()
        if not out:
            return ""
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                for field_name in ("result", "text", "output", "content", "response"):
                    val = parsed.get(field_name)
                    if isinstance(val, str):
                        return val.strip()
        except json.JSONDecodeError:
            pass
        return out

    return _invoke


def _apply_agent_patch_in_worktree(worktree_dir: Path, raw_response: str) -> None:
    parsed = _extract_json_block(raw_response)
    if parsed is not None and isinstance(parsed.get("files"), list):
        root_resolved = worktree_dir.resolve()
        for entry in parsed["files"]:
            if not isinstance(entry, dict):
                continue
            rel = str(entry.get("path", "")).strip()
            content = entry.get("content")
            if not rel or not isinstance(content, str):
                continue
            target = (root_resolved / rel).resolve()
            if root_resolved not in target.parents and target != root_resolved:
                raise ValueError(f"Patch path escapes worktree: {rel!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return

    diff_text = raw_response.strip()
    fenced_diff = re.search(r"```(?:diff|patch)?\s*(.*?)```", diff_text, re.DOTALL)
    if fenced_diff:
        diff_text = fenced_diff.group(1).strip()
    if diff_text:
        proc = subprocess.run(
            ["git", "-C", str(worktree_dir), "apply", "--whitespace=nowarn", "-"],
            input=diff_text + "\n",
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git_apply_failed:{proc.stderr.strip()[:300]}")


def run_dgm_pr_coding_agent(
    cluster: FailureCluster,
    *,
    repo_root: Union[str, Path],
    coding_agent_fn: Callable[[str], str],
    base_ref: str = "HEAD",
    open_draft_pr: bool = False,
    run_checks: bool = True,
    egress_policy: Optional[EgressPolicy] = None,
    ledger_path: Optional[Union[str, Path]] = None,
    check_commands: Optional[Sequence[Sequence[str]]] = None,
) -> KernelPRProposal:
    """Run the outer-loop coding agent in an isolated git worktree and return a human-gated KernelPRProposal."""
    repo_path = Path(repo_root).resolve()
    base_pol = egress_policy or EgressPolicy()
    pol = EgressPolicy(
        allowed_tokens=base_pol.allowed_tokens,
        include_html_aria_vocab=base_pol.include_html_aria_vocab,
        include_clinical_vocab=base_pol.include_clinical_vocab,
        extra_allowed_tokens=base_pol.extra_allowed_tokens | _DGM_EXTRA_TOKENS,
        allowed_var_keys=base_pol.allowed_var_keys,
    )
    if ledger_path is None:
        ledger_file = Path(tempfile.mkdtemp(prefix="oa_dgm_egress_")) / "outbound_egress_ledger.jsonl"
    else:
        ledger_file = Path(ledger_path)
    guard = LocalEgressGuard(ledger_file, policy=pol)

    cluster_payload: Dict[str, Any] = {
        "failure_category": cluster.failure_category,
        "normalized_signature": cluster.normalized_signature,
        "occurrence_count": cluster.occurrence_count,
        "sample_tokens": list(cluster.sample_tokens),
    }
    verdict = guard.check_and_record(cluster_payload, destination="dgm_pr_coding_agent")
    if not verdict.allowed:
        raise EgressBlockedError(verdict)

    prompt = (
        "Generate a minimal, general kernel fix and a regression test for this anonymized recurring failure cluster.\n"
        "Rules: never weaken verifier or egress checks, include at least one test file under tests/test_*.py, "
        "never reference specific app names, and never import legacy engines.\n"
        "Respond ONLY with JSON: {\"files\": [{\"path\": \"relative/path.py\", \"content\": \"...\"}], \"summary\": \"...\"} "
        "or a unified git diff.\n"
        f"Cluster JSON: {json.dumps(cluster_payload, sort_keys=True)}\n"
    )

    tmp_parent = Path(tempfile.mkdtemp(prefix="oa_dgm_worktree_"))
    wt_dir = tmp_parent / "wt"
    branch_name = f"dgm-pr/{cluster.cluster_id[:12]}-{int(time.time())}"
    pushed_branch = False

    add_proc = subprocess.run(
        ["git", "-C", str(repo_path), "worktree", "add", "-b", branch_name, str(wt_dir), base_ref],
        text=True,
        capture_output=True,
        check=False,
    )
    if add_proc.returncode != 0:
        shutil.rmtree(tmp_parent, ignore_errors=True)
        raise RuntimeError(f"git_worktree_add_failed:{add_proc.stderr.strip()[:300]}")

    try:
        raw_reply = coding_agent_fn(prompt)
        _apply_agent_patch_in_worktree(wt_dir, raw_reply)

        subprocess.run(["git", "-C", str(wt_dir), "add", "-A"], check=True, capture_output=True, text=True)
        diff_proc = subprocess.run(
            ["git", "-C", str(wt_dir), "diff", "--cached"],
            check=True,
            capture_output=True,
            text=True,
        )
        names_proc = subprocess.run(
            ["git", "-C", str(wt_dir), "diff", "--cached", "--name-only"],
            check=True,
            capture_output=True,
            text=True,
        )
        diff_text = diff_proc.stdout
        changed_files = [ln.strip() for ln in names_proc.stdout.splitlines() if ln.strip()]

        safety_ok, violations = validate_kernel_pr_safety(diff_text, changed_files=changed_files)
        check_results: List[Dict[str, Any]] = []
        checks_passed = not run_checks

        if run_checks and safety_ok:
            pkg_dir = wt_dir / "packages" / "kernel"
            check_cwd = pkg_dir if pkg_dir.exists() else wt_dir
            cmds: Sequence[Sequence[str]] = (
                check_commands
                if check_commands is not None
                else ([sys.executable, "-m", "pytest", "-q"],)
            )
            checks_passed = True
            for c_args in cmds:
                c_env = dict(os.environ)
                src_dir = check_cwd / "src"
                if src_dir.exists():
                    c_env["PYTHONPATH"] = str(src_dir)
                c_proc = subprocess.run(
                    list(c_args),
                    cwd=str(check_cwd),
                    env=c_env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                ok_cmd = c_proc.returncode == 0
                check_results.append(
                    {
                        "cmd": list(c_args),
                        "returncode": c_proc.returncode,
                        "ok": ok_cmd,
                        "output_tail": (c_proc.stdout + "\n" + c_proc.stderr).strip()[-400:],
                    }
                )
                if not ok_cmd:
                    checks_passed = False

        verification_summary: Dict[str, Any] = {
            "egress_ledger_entry_sha256": verdict.entry_sha256,
            "egress_payload_sha256": verdict.payload_sha256,
            "checks_ran": bool(run_checks and safety_ok),
            "checks_passed": bool(checks_passed),
            "check_results": check_results,
            "branch": branch_name,
            "draft_pr_url": "",
        }
        proposal = draft_kernel_pr_proposal(
            cluster,
            patch_diff=diff_text,
            changed_files=changed_files,
            verification_summary=verification_summary,
        )

        if open_draft_pr and proposal.safety_ok and checks_passed:
            subprocess.run(
                ["git", "-C", str(wt_dir), "commit", "-m", proposal.title],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(wt_dir), "push", "-u", "origin", branch_name],
                check=True,
                capture_output=True,
                text=True,
            )
            pushed_branch = True
            pr_proc = subprocess.run(
                ["gh", "pr", "create", "--draft", "--title", proposal.title, "--body", proposal.summary],
                cwd=str(wt_dir),
                text=True,
                capture_output=True,
                check=False,
            )
            if pr_proc.returncode == 0:
                verification_summary["draft_pr_url"] = pr_proc.stdout.strip()
        return proposal
    finally:
        subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(wt_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        if not pushed_branch:
            subprocess.run(
                ["git", "-C", str(repo_path), "branch", "-D", branch_name],
                capture_output=True,
                text=True,
                check=False,
            )
        shutil.rmtree(tmp_parent, ignore_errors=True)
