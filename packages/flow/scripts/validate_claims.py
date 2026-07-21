#!/usr/bin/env python3
"""Claim -> evidence validator: make maturity words a FUNCTION of tests.

`scripts/check_consistency.py` stops the README from carrying stale *strings*.
This script stops it from carrying stale *maturity claims*. It reads the
machine-readable registry `claims.yaml` (each claim -> a `tier` -> the backing
test(s)/benchmark(s)) and enforces a tier<->evidence contract, so a "supported"
claim whose proof is only an opt-in/infra-gated test — or is missing entirely —
is a hard CI failure instead of a thing a design partner discovers.

The evidence STRENGTH of each artifact is derived from the repo, never asserted
by the registry (which therefore cannot lie about it):

* a test file with NO module-level env skipif, that exists  -> ``supported``
  (it actually runs, and can be green, on the default CI suite)
* a test file gated by a module-level ``pytestmark`` env skipif -> ``validating``
  (opt-in / infra-gated: grounded in a real proof, but never on default CI)
* a doc / benchmark artifact (``.md`` or a benchmark dir)    -> ``roadmap``
  (design / field evidence; cannot by itself prove a running capability)

A claim FAILS when its ``tier`` OUTRANKS its strongest evidence, or when a
claim marked ``reproducibility: field`` is labeled ``supported`` (a result that
is not CI-reproducible is never presented as "supported"), or when any evidence
path is missing (registry rot).

Usage::

    python scripts/validate_claims.py --check      # gate (exit 1 on violation)
    python scripts/validate_claims.py --report      # (re)write docs/VERIFICATION.md + .json
    python scripts/validate_claims.py --check --junit runs/ci/junit.xml   # + green-check

The public functions are importable so ``tests/test_validate_claims.py`` can
drive them with controlled registries (catching registry rot before CI does).
"""

from __future__ import annotations

import argparse
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
STRENGTH_CI = "supported"  # non-opt-in test that exists -> runs on default CI
STRENGTH_OPTIN = "validating"  # opt-in / infra-gated test -> grounded, not on CI
STRENGTH_DOC = "roadmap"  # doc/benchmark artifact -> design/field evidence only
STRENGTH_MISSING = "research"  # nothing backing it


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
    junit_status: Optional[str] = None  # "passed" | "failed" | None (unknown)


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


def _classify_evidence(
    ev: dict[str, Any], repo_root: Path, junit: Optional[dict[str, str]]
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
                "post-merge/nightly full suite"
                if path.startswith("tests/e2e/")
                else "required PR gate (test)"
            )
            gating = f"ci ({stage})"
            if junit is not None:
                junit_status = junit.get(Path(path).name, "unknown")
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
        junit_status=junit_status,
    )


def validate_claim(
    raw: dict[str, Any],
    repo_root: Path = REPO_ROOT,
    junit: Optional[dict[str, str]] = None,
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
        result.evidence.append(_classify_evidence(ev, repo_root, junit))

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

    # 4) green-check (only when a junit artifact is supplied): a supported
    #    claim's CI tests must not be red.
    if junit is not None and tier == "supported":
        for e in result.evidence:
            if e.strength == STRENGTH_CI and e.junit_status == "failed":
                result.errors.append(
                    f"[{cid}] supported claim's backing test is RED in junit: {e.path}"
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
) -> list[ClaimResult]:
    return [
        validate_claim(raw, repo_root=repo_root, junit=junit)
        for raw in registry.get("claims", [])
    ]


# --------------------------------------------------------------------------- #
# optional junit parse (confirm supported claims are green)
# --------------------------------------------------------------------------- #
def parse_junit(path: Path) -> dict[str, str]:
    """Map test-file basename -> "passed"|"failed" from a junit XML artifact.

    Best-effort and coarse (file granularity): if ANY case in a file failed or
    errored, the file is "failed". Used only to red-flag a `supported` claim.
    """
    import xml.etree.ElementTree as ET

    status: dict[str, str] = {}
    root = ET.parse(path).getroot()
    for case in root.iter("testcase"):
        file_attr = case.get("file") or case.get("classname", "")
        name = Path(file_attr).name if file_attr else ""
        if not name.endswith(".py"):
            continue
        failed = any(child.tag in ("failure", "error") for child in case)
        prev = status.get(name)
        if failed:
            status[name] = "failed"
        elif prev != "failed":
            status[name] = "passed"
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
    "supported": "supported — CI-proven today",
    "validating": "validating — opt-in / infra-gated or field test",
    "roadmap": "roadmap — designed, not yet proven",
    "research": "research — open question",
}


def render_markdown(results: list[ClaimResult], now: str, junit_used: bool) -> str:
    lines: list[str] = []
    lines.append("# VERIFICATION — maturity claims backed by tests")
    lines.append("")
    lines.append(
        "> GENERATED by `scripts/validate_claims.py --report` from `claims.yaml`. "
        "Do not edit by hand — edit the registry and regenerate."
    )
    lines.append("")
    lines.append(f"- Generated at: **{now}**")
    lines.append(
        "- Green-check against a junit artifact: "
        + ("**run**" if junit_used else "**not run** (no `--junit` artifact supplied)")
    )
    lines.append(
        "- Gate: `python scripts/validate_claims.py --check` "
        "(a claim whose tier outranks its strongest backing evidence fails CI)."
    )
    lines.append("")
    lines.append(
        "**What this harness does and does not do.** It makes each public "
        "maturity claim a *function* of automated evidence: a `supported` claim "
        "must be backed by a test that actually runs on the default (non-opt-in) "
        "CI suite; a `validating` claim must be grounded in a REAL opt-in / "
        "infra-gated proof or a field test, and is never presented as "
        "supported. It does NOT replace the human half — third-party "
        "design-partner validation of the `validating` surfaces (Windows, "
        "Citrix) is exactly the evidence this repository cannot self-generate."
    )
    lines.append("")

    # honesty summary
    ci = [r for r in results if r.tier == "supported"]
    val = [r for r in results if r.tier == "validating"]
    other = [r for r in results if r.tier in ("roadmap", "research")]
    lines.append("## What is CI-proven today vs. being validated")
    lines.append("")
    lines.append(
        f"- **CI-proven today ({len(ci)}):** " + ", ".join(f"`{r.id}`" for r in ci)
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

    return "\n".join(lines) + "\n"


def render_json(
    results: list[ClaimResult], now: str, junit_used: bool
) -> dict[str, Any]:
    return {
        "generated_at": now,
        "green_check_run": junit_used,
        "ok": all(r.ok for r in results),
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
    p = Path(junit_path)
    if not p.exists():
        print(f"warning: --junit artifact not found, skipping green-check: {p}")
        return None
    return parse_junit(p)


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
        help="optional junit XML to confirm supported claims are green",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="ISO timestamp for the report (else OAFLOW_CLAIMS_NOW / git HEAD)",
    )
    args = parser.parse_args(argv)

    if not (args.check or args.report):
        args.check = True  # default action is the gate

    registry = load_registry(Path(args.registry))
    junit = _collect_junit(args.junit)
    results = validate_all(registry, junit=junit)
    now = resolve_now(args.now)

    if args.report:
        DOC_OUT.parent.mkdir(parents=True, exist_ok=True)
        DOC_OUT.write_text(
            render_markdown(results, now, junit is not None), encoding="utf-8"
        )
        JSON_OUT.write_text(
            json.dumps(render_json(results, now, junit is not None), indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"wrote {DOC_OUT.relative_to(REPO_ROOT)} and {JSON_OUT.relative_to(REPO_ROOT)}"
        )

    errors = [err for r in results for err in r.errors]
    if args.check:
        if errors:
            print(f"Claims gate FAILED ({len(errors)} violation(s)):")
            for err in errors:
                print(f"  - {err}")
            return 1
        n = len(results)
        proven = sum(1 for r in results if r.tier == "supported")
        print(
            f"Claims gate passed: {n} claims, {proven} supported (CI-proven), "
            "each tier backed by evidence of at least equal strength."
        )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
