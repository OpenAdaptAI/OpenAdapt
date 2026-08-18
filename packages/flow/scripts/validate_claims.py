#!/usr/bin/env python3
"""Claim -> evidence validator: make maturity words a FUNCTION of tests.

`scripts/check_consistency.py` stops the README from carrying stale *strings*.
This script stops it from carrying stale *maturity claims*. It reads the
machine-readable registry `claims.yaml` (each claim -> a `tier` -> the backing
test(s)/benchmark(s)) and enforces a tier<->evidence contract. A "supported"
claim whose proof is only an opt-in/infra-gated test, is missing, is absent from
the required job's JUnit result, is skipped, or fails is a hard CI failure.

The evidence STRENGTH of each artifact is derived from the repo, never asserted
by the registry (which therefore cannot lie about it):

* a test file with NO module-level env skipif, that exists  -> ``supported``
  candidate evidence (the required ``test`` or ``e2e-browser`` job must also
  supply a JUnit result that proves the cited file actually passed)
* a test file gated by a module-level ``pytestmark`` env skipif -> ``validating``
  (opt-in / infra-gated: grounded in a real proof, but never on default CI)
* a doc / benchmark artifact (``.md`` or a benchmark dir)    -> ``roadmap``
  (design / field evidence; cannot by itself prove a running capability)

A claim FAILS when its ``tier`` OUTRANKS its strongest evidence, when a claim
marked ``reproducibility: field`` is labeled ``supported`` (a result that is not
CI-reproducible is never presented as "supported"), or when any evidence path
is missing (registry rot). In a required test job, it also fails unless every
supported test assigned to that job has at least one passing case, no failing
case, and a real entry in that job's JUnit result.

Usage::

    python scripts/validate_claims.py --check --structure-only
    python scripts/validate_claims.py --check --ci-job test \
        --junit runs/unit-claims-junit.xml
    python scripts/validate_claims.py --report --ci-job validating \
        --junit runs/validating-junit.xml \
        --evidence-path tests/e2e/test_parallels_desktop_e2e.py
    python scripts/validate_claims.py --report      # (re)write docs/VERIFICATION.md + .json

The public functions are importable so ``tests/test_validate_claims.py`` can
drive them with controlled registries (catching registry rot before CI does).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY = REPO_ROOT / "claims.yaml"
DOC_OUT = REPO_ROOT / "docs" / "VERIFICATION.md"
JSON_OUT = REPO_ROOT / "docs" / "verification.json"

# Maturity tiers, weakest -> strongest. A claim's tier must not outrank the
# strongest evidence backing it.
TIER_RANK = {"research": 0, "roadmap": 1, "validating": 2, "supported": 3}
VALID_TIERS = set(TIER_RANK)

# Evidence strength labels reuse the tier vocabulary (same rank scale).
STRENGTH_CI = "supported"  # eligible test; required-job JUnit must prove its pass
STRENGTH_OPTIN = "validating"  # opt-in / infra-gated test -> grounded, not on CI
STRENGTH_DOC = "roadmap"  # doc/benchmark artifact -> design/field evidence only
STRENGTH_MISSING = "research"  # nothing backing it
CI_JOBS = {"test", "e2e-browser", "validating"}


# --------------------------------------------------------------------------- #
# opt-in detection (derived from the test's own source, not the registry)
# --------------------------------------------------------------------------- #
_ENV_FLAG = re.compile(
    r"""^(?P<flag>\w+)\s*=\s*os\.environ\.get\(\s*["'](?P<env>\w+)["']""",
    re.MULTILINE,
)
_PYTESTMARK = re.compile(r"^\s*pytestmark\s*=", re.MULTILINE)


def detect_optin_env(source: str) -> Optional[str]:
    """Return the env-var name a test module is OPT-IN gated on, else None.

    Opt-in == a MODULE-LEVEL ``pytestmark`` skipif on an env flag (skips ALL
    tests in the module unless the env var is set), the pattern the desktop and
    Citrix e2e proofs use. A per-function ``@pytest.mark.skipif`` decorator (as
    in ``test_effect_fhir.py``, whose bulk still runs in CI) is deliberately
    NOT treated as opt-in — only whole-module gating is.
    """
    if not _PYTESTMARK.search(source):
        return None

    # Prefer syntax-aware detection so both of the supported module-level
    # forms are classified correctly:
    #
    #   RUN = os.environ.get("FLAG") == "1"
    #   pytestmark = pytest.mark.skipif(not RUN, ...)
    #
    # and:
    #
    #   pytestmark = pytest.mark.skipif(
    #       os.environ.get("FLAG") != "1", ...
    #   )
    #
    # Restrict the search to the value assigned to a module-level
    # ``pytestmark``. An environment-gated test function elsewhere in the file
    # must not make the whole module look opt-in.
    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = None
    if tree is not None:
        env_flags: dict[str, str] = {}
        pytestmark_values: list[ast.AST] = []

        def env_from_call(node: ast.AST) -> Optional[str]:
            if not isinstance(node, ast.Call) or not node.args:
                return None
            func = node.func
            is_environ_get = (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "os"
            )
            is_getenv = (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            )
            first = node.args[0]
            if (is_environ_get or is_getenv) and isinstance(first, ast.Constant):
                return first.value if isinstance(first.value, str) else None
            return None

        for statement in tree.body:
            value: Optional[ast.AST] = None
            targets: list[ast.expr] = []
            if isinstance(statement, ast.Assign):
                value = statement.value
                targets = list(statement.targets)
            elif isinstance(statement, ast.AnnAssign):
                value = statement.value
                targets = [statement.target]
            if value is None:
                continue
            names = [target.id for target in targets if isinstance(target, ast.Name)]
            if "pytestmark" in names:
                pytestmark_values.append(value)
            for name in names:
                for node in ast.walk(value):
                    if (env := env_from_call(node)) is not None:
                        env_flags[name] = env
                        break

        for value in pytestmark_values:
            for call in (
                node
                for node in ast.walk(value)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "skipif"
            ):
                if not call.args:
                    continue
                condition = call.args[0]
                for node in ast.walk(condition):
                    if (env := env_from_call(node)) is not None:
                        return env
                    if isinstance(node, ast.Name) and node.id in env_flags:
                        return env_flags[node.id]

    # Keep the deliberately narrow legacy pattern as a fail-safe for source
    # syntax that the running Python cannot parse.
    flags = {m.group("flag"): m.group("env") for m in _ENV_FLAG.finditer(source)}
    for flag, env in flags.items():
        # `pytestmark = [pytest.mark.skipif(not FLAG, ...)]`
        if re.search(r"skipif\(\s*\n?\s*not\s+" + re.escape(flag) + r"\b", source):
            return env
    return None


def _infer_kind(path: str) -> str:
    if path.startswith("tests/") and path.endswith(".py"):
        return "test"
    if path.endswith(".md"):
        return "doc"
    return "benchmark"


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #
@dataclass
class EvidenceResult:
    path: str
    kind: str
    proves: str
    exists: bool
    strength: str
    gating: str  # human-readable: "ci (required PR gate)", "opt-in (ENV)", ...
    node: Optional[str] = None
    node_found: Optional[bool] = None
    ci_job: Optional[str] = None
    junit_status: Optional[str] = None  # "passed" | "failed" | "skipped" | "unknown"


@dataclass
class ClaimResult:
    id: str
    claim: str
    tier: str
    surfaces: list[str]
    caveats: list[str]
    reproducibility: Optional[str]
    evidence: list[EvidenceResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def strongest(self) -> str:
        if not self.evidence:
            return STRENGTH_MISSING
        return max(
            (e.strength for e in self.evidence),
            key=lambda s: TIER_RANK[s],
        )

    @property
    def ok(self) -> bool:
        return not self.errors


# --------------------------------------------------------------------------- #
# core validation
# --------------------------------------------------------------------------- #
def load_registry(path: Path = REGISTRY) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "claims" not in data:
        raise ValueError(
            f"{path} is not a claims registry (missing top-level 'claims')"
        )
    return data


def _required_ci_job(path: str, strength: str) -> Optional[str]:
    if strength == STRENGTH_OPTIN:
        return "validating"
    if strength != STRENGTH_CI:
        return None
    return "e2e-browser" if path.startswith("tests/e2e/") else "test"


def _classify_evidence(
    ev: dict[str, Any],
    repo_root: Path,
    junit: Optional[dict[str, str]],
    ci_job: Optional[str],
) -> EvidenceResult:
    path = str(ev["path"])
    kind = ev.get("kind") or _infer_kind(path)
    proves = str(ev.get("proves", "")).strip()
    node = ev.get("node")
    abs_path = repo_root / path
    exists = abs_path.exists()

    strength = STRENGTH_MISSING
    gating = "missing"
    node_found: Optional[bool] = None
    required_ci_job: Optional[str] = None
    junit_status: Optional[str] = None

    if not exists:
        gating = "MISSING PATH"
    elif kind == "test":
        source = abs_path.read_text(encoding="utf-8")
        if node:
            node_found = bool(
                re.search(r"\bdef\s+" + re.escape(str(node)) + r"\b", source)
            )
        env = detect_optin_env(source)
        if env:
            strength = STRENGTH_OPTIN
            gating = f"opt-in ({env})"
        else:
            strength = STRENGTH_CI
            stage = (
                "required PR gate (e2e-browser)"
                if path.startswith("tests/e2e/")
                else "required PR gate (test)"
            )
            gating = f"ci ({stage})"
        required_ci_job = _required_ci_job(path, strength)
        if junit is not None and required_ci_job == ci_job:
            junit_status = junit.get(path, "unknown")
    else:
        # doc / benchmark artifact: design or field evidence, never a run proof.
        strength = STRENGTH_DOC
        gating = "artifact (doc/benchmark)"

    return EvidenceResult(
        path=path,
        kind=kind,
        proves=proves,
        exists=exists,
        strength=strength,
        gating=gating,
        node=str(node) if node else None,
        node_found=node_found,
        ci_job=required_ci_job,
        junit_status=junit_status,
    )


def validate_claim(
    raw: dict[str, Any],
    repo_root: Path = REPO_ROOT,
    junit: Optional[dict[str, str]] = None,
    ci_job: Optional[str] = None,
    evidence_scope: Optional[set[str]] = None,
) -> ClaimResult:
    """Validate a single registry entry, returning a ClaimResult with errors."""
    cid = str(raw.get("id", "<no-id>"))
    tier = str(raw.get("tier", "")).strip()
    reproducibility = raw.get("reproducibility")
    result = ClaimResult(
        id=cid,
        claim=str(raw.get("claim", "")).strip(),
        tier=tier,
        surfaces=list(raw.get("surfaces", [])),
        caveats=list(raw.get("caveats", [])),
        reproducibility=reproducibility,
    )

    if tier not in VALID_TIERS:
        result.errors.append(
            f"[{cid}] unknown tier {tier!r} (expected one of {sorted(VALID_TIERS)})"
        )
        return result

    for ev in raw.get("evidence", []) or []:
        result.evidence.append(_classify_evidence(ev, repo_root, junit, ci_job))

    # 1) registry rot: every evidence path must exist.
    for e in result.evidence:
        if not e.exists:
            result.errors.append(f"[{cid}] evidence path does not exist: {e.path}")
        if e.node and e.node_found is False:
            result.errors.append(
                f"[{cid}] evidence {e.path} has no `def {e.node}` (node rot)"
            )

    # 2) the core contract: tier must not outrank the strongest evidence.
    strongest = result.strongest
    if TIER_RANK[tier] > TIER_RANK[strongest]:
        result.errors.append(
            f"[{cid}] OVERCLAIM: tier {tier!r} outranks strongest evidence "
            f"{strongest!r}. " + _overclaim_hint(tier, result)
        )

    # 3) a not-CI-reproducible (field) result may never be labeled supported.
    if reproducibility == "field" and tier == "supported":
        result.errors.append(
            f"[{cid}] OVERCLAIM: reproducibility: field cannot be tier "
            f"'supported' (result is not CI-reproducible)"
        )

    # 4) Required-job proof. File existence makes a test eligible to back a
    #    claim. It does not prove that the test ran. Required supported jobs
    #    enforce every assigned file. A substrate-specific validating run
    #    enforces every file in its explicit scope and makes no claim about the
    #    other validating evidence.
    if junit is not None and ci_job is not None:
        for e in result.evidence:
            if e.ci_job != ci_job or e.strength not in {
                STRENGTH_CI,
                STRENGTH_OPTIN,
            }:
                continue
            required = (
                e.path in evidence_scope
                if evidence_scope is not None
                else tier == "supported"
            )
            if not required:
                continue
            if e.junit_status == "failed":
                result.errors.append(
                    f"[{cid}] {tier} claim's backing test is RED in "
                    f"{ci_job} JUnit: {e.path}"
                )
            elif e.junit_status == "skipped":
                result.errors.append(
                    f"[{cid}] {tier} claim's backing test was SKIPPED in "
                    f"{ci_job} JUnit: {e.path}"
                )
            elif e.junit_status != "passed":
                result.errors.append(
                    f"[{cid}] {tier} claim's backing test is ABSENT from "
                    f"{ci_job} JUnit: {e.path}"
                )

    return result


def _overclaim_hint(tier: str, result: ClaimResult) -> str:
    if tier == "supported":
        optin = [e.path for e in result.evidence if e.strength == STRENGTH_OPTIN]
        if optin:
            return (
                "A supported claim needs a non-opt-in test that runs on default "
                f"CI; these are opt-in/infra-gated: {optin}. Downgrade to "
                "'validating' or add a CI-run backing test."
            )
        return (
            "A supported claim needs at least one non-opt-in test that exists "
            "and runs on default CI."
        )
    return "Downgrade the tier or strengthen the evidence."


def validate_all(
    registry: dict[str, Any],
    repo_root: Path = REPO_ROOT,
    junit: Optional[dict[str, str]] = None,
    ci_job: Optional[str] = None,
    evidence_scope: Optional[set[str]] = None,
) -> list[ClaimResult]:
    return [
        validate_claim(
            raw,
            repo_root=repo_root,
            junit=junit,
            ci_job=ci_job,
            evidence_scope=evidence_scope,
        )
        for raw in registry.get("claims", [])
    ]


def validate_evidence_scope(
    results: list[ClaimResult],
    ci_job: Optional[str],
    evidence_scope: Optional[set[str]],
) -> list[str]:
    """Reject a scoped live check that names evidence outside its exact job."""

    if evidence_scope is None:
        return []
    eligible = {
        evidence.path
        for result in results
        for evidence in result.evidence
        if evidence.ci_job == ci_job
    }
    return [
        f"[{ci_job}] scoped evidence is not registered for this job: {path}"
        for path in sorted(evidence_scope - eligible)
    ]


# --------------------------------------------------------------------------- #
# JUnit parse (prove supported claims passed in their required CI job)
# --------------------------------------------------------------------------- #
class JunitEvidenceError(ValueError):
    """A required CI result is absent, malformed, or carries no test cases."""


def _junit_case_path(case: Any, repo_root: Path) -> Optional[str]:
    """Return a repo-relative Python test path from one JUnit testcase."""

    file_attr = str(case.get("file") or "").replace("\\", "/").lstrip("./")
    if file_attr:
        if "/tests/" in file_attr:
            file_attr = "tests/" + file_attr.split("/tests/", 1)[1]
        candidate = Path(file_attr)
        if candidate.is_absolute():
            try:
                candidate = candidate.relative_to(repo_root)
            except ValueError:
                return None
        normalized = candidate.as_posix()
        if normalized.startswith("tests/") and normalized.endswith(".py"):
            return normalized

    classname = str(case.get("classname") or "")
    parts = classname.split(".") if classname else []
    while len(parts) >= 2:
        candidate_path = "/".join(parts) + ".py"
        if (
            candidate_path.startswith("tests/")
            and (repo_root / candidate_path).is_file()
        ):
            return candidate_path
        parts.pop()
    return None


def parse_junit(path: Path, repo_root: Path = REPO_ROOT) -> dict[str, str]:
    """Map repo-relative test file -> passed, failed, or skipped.

    Pytest's default xUnit2 output omits the ``file`` attribute. Resolve its
    dotted ``classname`` against the repository instead of silently returning
    an empty map. A file is failed when any case failed or errored. It is passed
    when at least one case passed and no case failed. It is skipped only when
    every mapped case was skipped.
    """
    import xml.etree.ElementTree as ET

    if not path.is_file():
        raise JunitEvidenceError(f"required JUnit artifact is missing: {path}")
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise JunitEvidenceError(
            f"required JUnit artifact cannot be read: {path}: {exc}"
        ) from exc

    status: dict[str, str] = {}
    rank = {"skipped": 1, "passed": 2, "failed": 3}
    for case in root.iter("testcase"):
        test_path = _junit_case_path(case, repo_root)
        if test_path is None:
            continue
        child_tags = {child.tag.rsplit("}", 1)[-1] for child in case}
        case_status = (
            "failed"
            if child_tags & {"failure", "error"}
            else "skipped"
            if "skipped" in child_tags
            else "passed"
        )
        previous = status.get(test_path)
        if previous is None or rank[case_status] > rank[previous]:
            status[test_path] = case_status
    if not status:
        raise JunitEvidenceError(
            f"required JUnit artifact contains no repository test cases: {path}"
        )
    return status


# --------------------------------------------------------------------------- #
# timestamp (runtime forbids wall-clock in some contexts)
# --------------------------------------------------------------------------- #
def resolve_now(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    env = os.environ.get("OAFLOW_CLAIMS_NOW")
    if env:
        return env
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", "-s", "--format=%cI", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        stamp = out.stdout.strip()
        if stamp:
            return f"{stamp} (git HEAD commit date)"
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return "unknown (no --now, OAFLOW_CLAIMS_NOW, or git available)"


# --------------------------------------------------------------------------- #
# report generation
# --------------------------------------------------------------------------- #
_TIER_BADGE = {
    "supported": "supported — bound to required CI pass evidence",
    "validating": "validating — opt-in / infra-gated or field test",
    "roadmap": "roadmap — designed, not yet proven",
    "research": "research — open question",
}


def render_markdown(
    results: list[ClaimResult],
    now: str,
    junit_used: bool,
    junit_job: Optional[str] = None,
    junit_scope: Optional[set[str]] = None,
    validation_errors: Optional[list[str]] = None,
) -> str:
    lines: list[str] = []
    lines.append("# VERIFICATION — maturity claims backed by tests")
    lines.append("")
    lines.append(
        "> GENERATED by `scripts/validate_claims.py --report` from `claims.yaml`. "
        "Do not edit by hand — edit the registry and regenerate."
    )
    lines.append("")
    lines.append(f"- Generated at: **{now}**")
    if junit_used and junit_job:
        scope_text = (
            "; exact evidence scope: "
            + ", ".join(f"`{path}`" for path in sorted(junit_scope))
            if junit_scope
            else "; all supported evidence assigned to this job"
        )
        lines.append(f"- JUnit pass check: **run for `{junit_job}`{scope_text}**")
    else:
        lines.append(
            "- JUnit pass check: **not embedded in this generated registry view** "
            "(required CI jobs enforce pass evidence)"
        )
    lines.append(
        "- Structure gate: `python scripts/validate_claims.py --check "
        "--structure-only` (a claim whose tier outranks its strongest backing "
        "evidence fails CI)."
    )
    lines.append(
        "- Pass gates: required `test` and `e2e-browser` jobs supply their own "
        "JUnit files; an absent, all-skipped, or failed supported evidence file "
        "fails that required job."
    )
    lines.append(
        "- Scoped validating refreshes name each selected evidence file; each "
        "selected file must pass on its declared substrate. Unselected validating "
        "evidence is not represented as checked."
    )
    if validation_errors:
        lines.append("")
        lines.append("## Validation errors")
        lines.append("")
        for error in validation_errors:
            lines.append(f"- ❌ {error}")
    lines.append("")
    lines.append(
        "**What this harness does and does not do.** It makes each public "
        "maturity claim a *function* of automated evidence: a `supported` claim "
        "must be backed by a test file that has a real passing case in its "
        "required default CI job; a `validating` claim must be grounded in a REAL opt-in / "
        "infra-gated proof or a field test, and is never presented as "
        "supported. It does not replace workflow- and deployment-specific "
        "acceptance: application controls, identity rules, effect oracles, and "
        "live transport conditions remain bound to their counted evidence."
    )
    lines.append("")

    # honesty summary
    ci = [r for r in results if r.tier == "supported"]
    val = [r for r in results if r.tier == "validating"]
    other = [r for r in results if r.tier in ("roadmap", "research")]
    lines.append("## What is bound to required CI vs. being validated")
    lines.append("")
    lines.append(
        f"- **Bound to required CI pass evidence ({len(ci)}):** "
        + ", ".join(f"`{r.id}`" for r in ci)
    )
    lines.append(
        f"- **Being validated — opt-in / infra-gated or field ({len(val)}):** "
        + ", ".join(f"`{r.id}`" for r in val)
    )
    if other:
        lines.append(
            f"- **Roadmap / research ({len(other)}):** "
            + ", ".join(f"`{r.id}`" for r in other)
        )
    lines.append("")

    # per-claim detail
    lines.append("## Claims")
    lines.append("")
    for r in results:
        badge = _TIER_BADGE.get(r.tier, r.tier)
        lines.append(f"### `{r.id}` — {badge}")
        lines.append("")
        lines.append(f"> {r.claim}")
        lines.append("")
        if r.reproducibility:
            lines.append(f"- Reproducibility: **{r.reproducibility}**")
        lines.append(f"- Surfaces: {', '.join(r.surfaces) or '—'}")
        lines.append(
            f"- Strongest evidence strength: **{r.strongest}** (tier is `{r.tier}`)"
        )
        lines.append("")
        lines.append(
            "| Backing evidence | Kind | Gating / CI stage | Strength | Proves |"
        )
        lines.append("|---|---|---|---|---|")
        for e in r.evidence:
            proves = e.proves.replace("\n", " ").strip()
            lines.append(
                f"| `{e.path}` | {e.kind} | {e.gating} | {e.strength} | {proves} |"
            )
        lines.append("")
        if r.caveats:
            lines.append("**Caveats (honest limits):**")
            lines.append("")
            for c in r.caveats:
                lines.append(f"- {c.strip()}")
            lines.append("")
        if r.errors:
            lines.append("**GATE ERRORS:**")
            lines.append("")
            for err in r.errors:
                lines.append(f"- ❌ {err}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_json(
    results: list[ClaimResult],
    now: str,
    junit_used: bool,
    junit_job: Optional[str] = None,
    junit_scope: Optional[set[str]] = None,
    validation_errors: Optional[list[str]] = None,
) -> dict[str, Any]:
    validation_errors = validation_errors or []
    return {
        "generated_at": now,
        "green_check_run": junit_used,
        "green_check_job": junit_job,
        "green_check_scope": sorted(junit_scope or []),
        "ok": all(r.ok for r in results) and not validation_errors,
        "validation_errors": validation_errors,
        "claims": [
            {
                "id": r.id,
                "claim": r.claim,
                "tier": r.tier,
                "reproducibility": r.reproducibility,
                "surfaces": r.surfaces,
                "strongest_evidence": r.strongest,
                "caveats": r.caveats,
                "evidence": [
                    {
                        "path": e.path,
                        "kind": e.kind,
                        "exists": e.exists,
                        "strength": e.strength,
                        "gating": e.gating,
                        "node": e.node,
                        "node_found": e.node_found,
                        "ci_job": e.ci_job,
                        "junit_status": e.junit_status,
                        "proves": e.proves,
                    }
                    for e in r.evidence
                ],
                "errors": r.errors,
            }
            for r in results
        ],
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _collect_junit(junit_path: Optional[str]) -> Optional[dict[str, str]]:
    if not junit_path:
        return None
    return parse_junit(Path(junit_path))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="gate: exit 1 on any violation"
    )
    parser.add_argument(
        "--report", action="store_true", help="regenerate docs/VERIFICATION.md + .json"
    )
    parser.add_argument("--registry", default=str(REGISTRY), help="path to claims.yaml")
    parser.add_argument(
        "--junit",
        default=None,
        help="JUnit XML from the required job named by --ci-job",
    )
    parser.add_argument(
        "--ci-job",
        choices=sorted(CI_JOBS),
        default=None,
        help="required CI job that produced --junit",
    )
    parser.add_argument(
        "--evidence-path",
        action="append",
        default=[],
        help=(
            "exact validating evidence path selected by this substrate-specific "
            "run; repeat for each selected file"
        ),
    )
    parser.add_argument(
        "--structure-only",
        action="store_true",
        help="check registry structure without claiming that supported tests passed",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="ISO timestamp for the report (else OAFLOW_CLAIMS_NOW / git HEAD)",
    )
    args = parser.parse_args(argv)

    if not (args.check or args.report):
        args.check = True  # default action is the gate

    if args.structure_only and (args.junit or args.ci_job or args.evidence_path):
        print(
            "Claims gate FAILED: --structure-only cannot consume a JUnit result "
            "or evidence scope"
        )
        return 1
    if bool(args.junit) != bool(args.ci_job):
        print("Claims gate FAILED: --junit and --ci-job must be supplied together")
        return 1
    if args.check and not args.structure_only and not args.junit:
        print(
            "Claims gate FAILED: a supported-tier check requires --junit and "
            "--ci-job; use --structure-only only for the separate registry-shape gate"
        )
        return 1
    evidence_scope = set(args.evidence_path) if args.evidence_path else None
    if args.ci_job == "validating" and evidence_scope is None:
        print(
            "Claims gate FAILED: --ci-job validating requires one or more exact "
            "--evidence-path values"
        )
        return 1
    if args.ci_job in {"test", "e2e-browser"} and evidence_scope is not None:
        print(
            "Claims gate FAILED: required supported CI jobs derive their complete "
            "evidence scope from claims.yaml"
        )
        return 1

    registry = load_registry(Path(args.registry))
    try:
        junit = _collect_junit(args.junit)
    except JunitEvidenceError as exc:
        print(f"Claims gate FAILED: {exc}")
        return 1
    results = validate_all(
        registry,
        junit=junit,
        ci_job=args.ci_job,
        evidence_scope=evidence_scope,
    )
    scope_errors = validate_evidence_scope(results, args.ci_job, evidence_scope)
    now = resolve_now(args.now)

    if args.report:
        DOC_OUT.parent.mkdir(parents=True, exist_ok=True)
        DOC_OUT.write_text(
            render_markdown(
                results,
                now,
                junit is not None,
                args.ci_job,
                evidence_scope,
                scope_errors,
            ),
            encoding="utf-8",
        )
        JSON_OUT.write_text(
            json.dumps(
                render_json(
                    results,
                    now,
                    junit is not None,
                    args.ci_job,
                    evidence_scope,
                    scope_errors,
                ),
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(
            f"wrote {DOC_OUT.relative_to(REPO_ROOT)} and {JSON_OUT.relative_to(REPO_ROOT)}"
        )

    errors = [err for r in results for err in r.errors] + scope_errors
    if args.check:
        if errors:
            print(f"Claims gate FAILED ({len(errors)} violation(s)):")
            for err in errors:
                print(f"  - {err}")
            return 1
        n = len(results)
        proven = sum(1 for r in results if r.tier == "supported")
        print(
            f"Claims gate passed: {n} claims, {proven} marked supported; "
            + (
                (
                    f"all scoped {args.ci_job} claim evidence passed."
                    if evidence_scope is not None
                    else f"all {args.ci_job} claim evidence passed."
                )
                if args.ci_job
                else "registry structure is consistent; no live pass was claimed."
            )
        )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
