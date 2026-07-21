"""Read-only projections of on-disk engine artifacts for the operator console.

Everything here READS artifacts the engine already writes -- bundle
directories (:meth:`openadapt_flow.ir.Workflow.load`), run directories
(``report.json`` / ``pending_escalation.json`` / ``approval.json`` /
``checkpoints/``), and skill libraries (``skills.json``) -- and computes
coverage with the SAME helpers the ``lint`` / ``certify`` CLI verbs use.
Nothing in this module writes to disk and nothing invents a new metric the
engine does not already define.

Degradation is deliberate and graceful: an encrypted bundle without a key, a
corrupt ``workflow.json``, or a report predating a field yields a summary with
``load_error`` / ``None`` fields instead of an exception, so one bad artifact
never blanks the operator's inventory.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from openadapt_flow.ir import RunReport, Step, Workflow
from openadapt_flow.policy import (
    CertifyReport,
    builtin_policy_names,
    evaluate_policy,
    has_system_effect,
    is_identity_applicable,
    is_identity_armed,
    lint_workflow,
    load_policy,
    step_confidence,
)
from openadapt_flow.traversal import iter_workflow_steps

#: Directory-scan depth for bundle / run roots: direct children plus one more
#: level (covers ``runs/replay-*`` and bench roots like ``bench/iter-*/``).
_SCAN_DEPTH = 2
_SAFE_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$")
_SAFE_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")


def _safe_timestamp(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _safe_version(value: Any) -> Optional[str]:
    return (
        value if isinstance(value, str) and _SAFE_VERSION_RE.fullmatch(value) else None
    )


def _safe_digest(value: Any) -> Optional[str]:
    return (
        value if isinstance(value, str) and _SAFE_DIGEST_RE.fullmatch(value) else None
    )


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------


def _is_bundle_dir(path: Path) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    plain = path / "workflow.json"
    encrypted = path / "workflow.json.enc"
    return (plain.is_file() and not plain.is_symlink()) or (
        encrypted.is_file() and not encrypted.is_symlink()
    )


def _is_run_dir(path: Path) -> bool:
    report = path / "report.json"
    return (
        not path.is_symlink()
        and path.is_dir()
        and report.is_file()
        and not report.is_symlink()
    )


def _scan(root: Path, predicate: Any, depth: int = _SCAN_DEPTH) -> list[Path]:
    """Directories under ``root`` (including ``root`` itself) matching
    ``predicate``, breadth-first to ``depth`` levels, sorted by path."""
    out: list[Path] = []
    if not root.is_dir():
        return out
    frontier = [root]
    for _ in range(depth + 1):
        nxt: list[Path] = []
        for d in frontier:
            if predicate(d):
                out.append(d)
                continue  # a bundle/run dir does not nest another
            try:
                # A configured root is a security boundary.  Path.is_dir()
                # follows directory symlinks, so test is_symlink() first and
                # never traverse an alias to a directory outside the root.
                nxt.extend(
                    c for c in sorted(d.iterdir()) if not c.is_symlink() and c.is_dir()
                )
            except OSError:
                continue
        frontier = nxt
    return sorted(out)


def _rel_id(root: Path, path: Path) -> str:
    """Stable opaque id that never exports a possibly sensitive path name."""
    relative = "." if path == root else path.relative_to(root).as_posix()
    return hashlib.sha256(relative.encode("utf-8")).hexdigest()[:24]


def resolve_scanned_dir(root: Path, entry_id: str, predicate: Any) -> Optional[Path]:
    """Resolve an id BACK to a scanned directory, refusing anything that is
    not exactly one of the directories a fresh scan yields (so a crafted id
    can never escape the root)."""
    for d in _scan(root, predicate):
        if entry_id == _rel_id(root, d):
            try:
                resolved = d.resolve(strict=True)
                resolved.relative_to(root.resolve(strict=True))
            except (OSError, ValueError):
                return None
            if resolved != d.absolute():
                return None
            return resolved
    return None


# ---------------------------------------------------------------------------
# bundle projections
# ---------------------------------------------------------------------------


class IdentityCoverage(BaseModel):
    applicable: int = 0
    armed: int = 0
    unarmed: list[dict[str, str]] = Field(default_factory=list)


class EffectCoverage(BaseModel):
    """Effect-verification coverage: how many CONSEQUENTIAL (irreversible)
    actions carry a declared system-of-record contract (``Step.effects``).

    ``coverage_pct`` is None when the bundle has no irreversible step -- the
    UI shows "n/a" instead of a fabricated 100%.
    """

    consequential: int = 0
    consequential_with_contract: int = 0
    steps_with_contract: int = 0
    coverage_pct: Optional[float] = None


class BundleSummary(BaseModel):
    id: str
    schema_version: Optional[int] = None
    compiler_version: Optional[str] = None
    created_at: Optional[str] = None
    content_digest: Optional[str] = None
    certified: Optional[bool] = None
    certification_status: Optional[str] = None
    policy_name: Optional[str] = None
    certified_at: Optional[str] = None
    expires_at: Optional[str] = None
    contains_phi: Optional[bool] = None
    phi_scrubbed: Optional[bool] = None
    encrypted: bool = False
    n_steps: Optional[int] = None
    load_error: Optional[str] = None
    last_run: Optional[dict[str, Any]] = None


def load_workflow_safe(path: Path) -> tuple[Optional[Workflow], Optional[str]]:
    """``Workflow.load`` that degrades to ``(None, reason)`` instead of
    raising -- encrypted-without-key, tampered, or corrupt bundles stay
    listable."""
    try:
        return Workflow.load(path), None
    except Exception as e:  # noqa: BLE001 - inventory must never blank
        # Loader exceptions can echo corrupt JSON/YAML input, filesystem paths,
        # or decrypted values.  The detailed exception belongs in a protected
        # local log, not the browser/API response.
        return None, f"{type(e).__name__}: bundle could not be loaded safely"


def bundle_summary(root: Path, path: Path) -> BundleSummary:
    encrypted_on_disk = (path / "workflow.json.enc").is_file()
    wf, err = load_workflow_safe(path)
    if wf is None:
        return BundleSummary(
            id=_rel_id(root, path),
            encrypted=encrypted_on_disk,
            load_error=err,
        )
    prov = wf.manifest.provenance if wf.manifest else None
    return BundleSummary(
        id=_rel_id(root, path),
        schema_version=wf.schema_version,
        compiler_version=_safe_version(prov.compiler_version) if prov else None,
        created_at=_safe_timestamp(prov.created_at if prov else wf.created_at),
        content_digest=_safe_digest(
            wf.manifest.content_digest if wf.manifest else None
        ),
        certified=prov.certified if prov else None,
        certification_status=(
            prov.certification_status
            if prov and prov.certification_status in {"certified", "failed", "expired"}
            else None
        ),
        policy_name=(
            prov.policy_name
            if prov and prov.policy_name in builtin_policy_names()
            else ("custom" if prov and prov.policy_name else None)
        ),
        certified_at=_safe_timestamp(prov.certified_at) if prov else None,
        expires_at=_safe_timestamp(prov.expires_at) if prov else None,
        contains_phi=wf.contains_phi,
        phi_scrubbed=wf.phi_scrubbed,
        encrypted=wf.encrypted or encrypted_on_disk,
        n_steps=len(list(iter_workflow_steps(wf))),
        load_error=None,
    )


def list_bundles(root: Path) -> list[BundleSummary]:
    return [bundle_summary(root, p) for p in _scan(root, _is_bundle_dir)]


def identity_coverage(wf: Workflow) -> IdentityCoverage:
    cov = IdentityCoverage()
    for index, step in enumerate(iter_workflow_steps(wf), start=1):
        if not is_identity_applicable(step):
            continue
        cov.applicable += 1
        if is_identity_armed(step):
            cov.armed += 1
        else:
            cov.unarmed.append(
                {
                    "step_id": f"step-{index:03d}",
                    "intent": "recorded label retained in protected bundle",
                    "reason": "identity evidence unavailable",
                }
            )
    return cov


def effect_coverage(wf: Workflow) -> EffectCoverage:
    cov = EffectCoverage()
    for step in iter_workflow_steps(wf):
        if has_system_effect(step):
            cov.steps_with_contract += 1
        if step.risk == "irreversible":
            cov.consequential += 1
            if has_system_effect(step):
                cov.consequential_with_contract += 1
    if cov.consequential:
        cov.coverage_pct = round(
            100.0 * cov.consequential_with_contract / cov.consequential, 1
        )
    return cov


def _step_projection(step: Step, index: int) -> dict[str, Any]:
    return {
        "id": f"step-{index:03d}",
        "intent": "recorded label retained in protected bundle",
        "action": step.action.value,
        "risk": step.risk,
        "secret": step.secret,
        "identity_applicable": is_identity_applicable(step),
        "identity_armed": (
            is_identity_armed(step) if is_identity_applicable(step) else None
        ),
        "identity_unarmed_reason": (
            "identity evidence unavailable"
            if is_identity_applicable(step) and not is_identity_armed(step)
            else None
        ),
        "n_effects": len(step.effects),
        "effects": [
            {
                "kind": e.kind.value,
                "expected_count": e.expected_count,
                "has_idempotency_key": bool(getattr(e, "idempotency_key", None)),
                "needs_operator_confirmation": bool(
                    getattr(e, "needs_operator_confirmation", False)
                ),
            }
            for e in step.effects
        ],
        "n_postconditions": len(step.expect),
        # Postcondition text can retain demonstrated identifiers.  The console
        # needs coverage/kind, not the literal audit value.
        "postconditions": [{"kind": p.kind.value} for p in step.expect],
        "confidence": round(step_confidence(step), 2),
        "parameterized": step.param is not None,
    }


def certification_view(
    wf: Workflow, policy_override: Optional[str] = None
) -> dict[str, Any]:
    """The bundle's SEALED certification block plus, when a policy is
    resolvable (the sealed ``policy_name``, an explicit override, or a
    builtin), a LIVE ``evaluate_policy`` pass with its violations.

    When no policy can be resolved the live block degrades to the exact
    ``certify`` command for the operator to run."""
    prov = wf.manifest.provenance if wf.manifest else None
    sealed_policy: Optional[str] = prov.policy_name if prov else None
    sealed = {
        "certified": prov.certified if prov else False,
        "status": (
            prov.certification_status
            if prov and prov.certification_status in {"certified", "failed", "expired"}
            else None
        ),
        "policy_name": (
            sealed_policy if sealed_policy in builtin_policy_names() else "custom"
        )
        if sealed_policy
        else None,
        "certified_at": _safe_timestamp(prov.certified_at) if prov else None,
        "expires_at": _safe_timestamp(prov.expires_at) if prov else None,
    }
    source = policy_override or sealed_policy
    live: Optional[dict[str, Any]] = None
    live_error: Optional[str] = None
    if source:
        if source not in builtin_policy_names():
            # A policy path embedded in an imported bundle must never turn a
            # GET request into an arbitrary local-file read.
            live_error = (
                "custom policy evaluation is available through the reviewed CLI"
            )
        else:
            try:
                report: CertifyReport = evaluate_policy(wf, load_policy(source))
                live = {
                    "policy_name": report.policy_name,
                    "passed": report.passed,
                    "violations": [
                        {"rule": violation.rule} for violation in report.violations
                    ],
                }
            except Exception as e:  # noqa: BLE001 - degrade, never 500 the page
                live_error = f"{type(e).__name__}: policy evaluation failed"
    return {
        "sealed": sealed,
        "live": live,
        "live_error": live_error,
        "available_policies": builtin_policy_names(),
    }


def bundle_detail(
    root: Path, path: Path, policy_override: Optional[str] = None
) -> dict[str, Any]:
    summary = bundle_summary(root, path)
    if summary.load_error:
        return {"summary": summary.model_dump(), "load_error": summary.load_error}
    wf, _ = load_workflow_safe(path)
    assert wf is not None  # summary.load_error was None
    steps = [
        _step_projection(step, index)
        for index, step in enumerate(iter_workflow_steps(wf), start=1)
    ]
    lint = lint_workflow(wf)
    return {
        "summary": summary.model_dump(),
        "parameter_count": len(set(wf.params) | set(wf.param_specs)),
        "secret_parameter_count": len(wf.secret_params),
        "program_mode": wf.program is not None,
        "steps": steps,
        "identity_coverage": identity_coverage(wf).model_dump(),
        "effect_coverage": effect_coverage(wf).model_dump(),
        "lint": {
            "findings": [
                {"severity": finding.severity, "code": finding.code}
                for finding in lint.findings
            ]
        },
        "certification": certification_view(wf, policy_override),
    }


# ---------------------------------------------------------------------------
# bundle diff
# ---------------------------------------------------------------------------


def _step_index(wf: Workflow) -> dict[str, Step]:
    return {s.id: s for s in iter_workflow_steps(wf)}


def bundle_diff(root: Path, path_a: Path, path_b: Path) -> dict[str, Any]:
    """Structural diff between two bundle directories: manifest-level fields
    plus steps added / removed / changed (by step id, comparing the serialized
    step models field-by-field)."""
    a_sum, b_sum = bundle_summary(root, path_a), bundle_summary(root, path_b)
    out: dict[str, Any] = {"a": a_sum.model_dump(), "b": b_sum.model_dump()}
    if a_sum.load_error or b_sum.load_error:
        out["error"] = "one or both bundles could not be loaded"
        return out
    wf_a, _ = load_workflow_safe(path_a)
    wf_b, _ = load_workflow_safe(path_b)
    assert wf_a is not None and wf_b is not None
    ia, ib = _step_index(wf_a), _step_index(wf_b)
    added = sorted(set(ib) - set(ia))
    removed = sorted(set(ia) - set(ib))
    changed_count = 0
    for sid in sorted(set(ia) & set(ib)):
        da = ia[sid].model_dump(mode="json")
        db = ib[sid].model_dump(mode="json")
        fields = sorted(k for k in set(da) | set(db) if da.get(k) != db.get(k))
        if fields:
            changed_count += 1
    out.update(
        {
            "params_changed": wf_a.params != wf_b.params,
            "steps_added_count": len(added),
            "steps_removed_count": len(removed),
            "steps_changed_count": changed_count,
            "identical": (
                not added
                and not removed
                and not changed_count
                and wf_a.params == wf_b.params
            ),
        }
    )
    return out


# ---------------------------------------------------------------------------
# run projections
# ---------------------------------------------------------------------------


class RunSummary(BaseModel):
    id: str
    started_at: Optional[str] = None
    success: Optional[bool] = None
    terminal_outcome: Optional[str] = None
    halted: bool = False
    paused: bool = False
    approved: bool = False
    n_results: int = 0
    n_failed: int = 0
    total_ms: Optional[float] = None
    identity_applicable_steps: Optional[int] = None
    identity_armed_steps: Optional[int] = None
    screenshots_may_leave_box: Optional[bool] = None
    bundle_content_digest: Optional[str] = None
    load_error: Optional[str] = None


def _load_report(path: Path) -> tuple[Optional[RunReport], Optional[str]]:
    try:
        report_file = _contained_file(path, path / "report.json")
        if report_file is None:
            raise OSError("report is not a contained regular file")
        raw = json.loads(report_file.read_text())
        return RunReport.model_validate(raw), None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: run report could not be loaded safely"


def _contained_file(root: Path, path: Path) -> Optional[Path]:
    """Return a regular file below root without traversing any symlink."""
    lexical_root = root.absolute()
    lexical = path.absolute()
    if lexical_root.is_symlink():
        return None
    try:
        relative = lexical.relative_to(lexical_root)
    except ValueError:
        return None
    current = lexical_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return None
    try:
        resolved_root = lexical_root.resolve(strict=True)
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _read_json_opt(
    path: Path, *, root: Optional[Path] = None
) -> Optional[dict[str, Any]]:
    safe = _contained_file(root or path.parent, path)
    if safe is None:
        return None
    try:
        data = json.loads(safe.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def run_summary(root: Path, path: Path) -> RunSummary:
    report, err = _load_report(path)
    paused = any(
        _contained_file(path, path / filename) is not None
        for filename in (
            "pending_escalation.json",
            "pending_escalation.json.enc",
        )
    )
    approved = any(
        _contained_file(path, path / filename) is not None
        for filename in ("approval.json", "approval.json.enc")
    )
    if report is None:
        return RunSummary(
            id=_rel_id(root, path),
            paused=paused,
            approved=approved,
            load_error=err,
        )
    return RunSummary(
        id=_rel_id(root, path),
        started_at=_safe_timestamp(report.started_at),
        success=report.success,
        terminal_outcome=(
            report.terminal_outcome
            if report.terminal_outcome
            in {"success", "halt", "escalate", "policy_refused"}
            else None
        ),
        halted=report.halt is not None,
        paused=paused,
        approved=approved,
        n_results=len(report.results),
        n_failed=sum(1 for r in report.results if not r.ok and not r.skipped),
        total_ms=report.total_ms,
        identity_applicable_steps=report.identity_applicable_steps,
        identity_armed_steps=report.identity_armed_steps,
        screenshots_may_leave_box=report.screenshots_may_leave_box,
        bundle_content_digest=_safe_digest(report.bundle_content_digest),
    )


def list_runs(root: Path) -> list[RunSummary]:
    runs = [run_summary(root, p) for p in _scan(root, _is_run_dir)]
    return sorted(runs, key=lambda r: r.started_at or "", reverse=True)


def latest_runs_by_digest(root: Path) -> dict[str, dict[str, Any]]:
    """Newest run summary per sealed bundle digest, without workflow names."""
    out: dict[str, dict[str, Any]] = {}
    for run in list_runs(root):  # already newest-first
        digest = run.bundle_content_digest
        if digest and digest not in out:
            out[digest] = {
                "run_id": run.id,
                "started_at": run.started_at,
                "success": run.success,
                "halted": run.halted,
                "paused": run.paused,
            }
    return out


def _checkpoints_listing(run_dir: Path) -> list[dict[str, Any]]:
    cdir = run_dir / "checkpoints"
    out: list[dict[str, Any]] = []
    if cdir.is_symlink() or not cdir.is_dir():
        return out
    for p in sorted(cdir.glob("step_*.json")):
        data = _read_json_opt(p, root=run_dir) or {}
        step_index = data.get("step_index")
        out.append(
            {
                "step_index": (
                    step_index
                    if isinstance(step_index, int) and step_index >= 0
                    else None
                ),
                "created_at": _safe_timestamp(data.get("created_at")),
            }
        )
    for p in sorted(cdir.glob("step_*.json.enc")):
        if _contained_file(run_dir, p) is not None:
            out.append({"encrypted": True})
    return out


def run_detail(root: Path, path: Path) -> dict[str, Any]:
    summary = run_summary(root, path)
    out: dict[str, Any] = {"summary": summary.model_dump()}
    report, _ = _load_report(path)
    if report is not None:
        out["timeline"] = [
            {
                "step_id": f"step-{index:03d}",
                "intent": "recorded label retained in protected report",
                "ok": r.ok,
                "skipped": r.skipped,
                "safety_halt": r.safety_halt,
                # Identity expected/observed values can be literal PHI.  Only
                # the verdict metadata needed by the operator UI crosses the
                # browser boundary.
                "identity": (
                    {
                        "status": r.identity.status,
                        "mode": r.identity.mode,
                        "coverage": r.identity.coverage,
                    }
                    if r.identity
                    else None
                ),
                "effect_verified": r.effect_verified,
                "effect_approved_unverified": r.effect_approved_unverified,
                # Human-readable effect lines may contain record selectors.
                "effect_results": (
                    ["effect verdict retained in protected report"]
                    if r.effect_results
                    else []
                ),
                "resolution_rung": r.resolution.rung if r.resolution else None,
                "error": (
                    "step error retained in protected report" if r.error else None
                ),
                "before_artifact_id": (
                    f"step-{index:03d}-before" if r.before_png else None
                ),
                "after_artifact_id": (
                    f"step-{index:03d}-after" if r.after_png else None
                ),
                "elapsed_ms": r.elapsed_ms,
            }
            for index, r in enumerate(report.results, start=1)
        ]
        out["halt"] = (
            {
                "outcome": (
                    report.halt.outcome
                    if report.halt.outcome in {"halt", "escalate"}
                    else "halt"
                ),
                "observed_text_count": len(report.halt.observed_texts),
                "completed_intent_count": len(report.halt.completed_intents),
            }
            if report.halt
            else None
        )
    pending = _read_json_opt(path / "pending_escalation.json", root=path)
    out["pending_escalation"] = (
        {
            "category": "operator_review",
            "status": (
                pending.get("status")
                if pending.get("status") in {"pending", "approved", "resolved"}
                else "unknown"
            ),
            "resume_from_index": (
                pending.get("resume_from_index")
                if isinstance(pending.get("resume_from_index"), int)
                else None
            ),
        }
        if pending
        else None
    )
    out["pending_escalation_encrypted"] = (
        path / "pending_escalation.json.enc"
    ).is_file()
    approval = _read_json_opt(path / "approval.json", root=path)
    out["approval"] = (
        {
            "present": True,
            "approved_at": _safe_timestamp(approval.get("approved_at")),
        }
        if approval
        else None
    )
    out["checkpoints"] = _checkpoints_listing(path)
    manifest = _read_json_opt(path / "checkpoints" / "_manifest.json", root=path)
    out["manifest"] = {"present": True} if manifest else None
    return out


def _report_image_refs(run_dir: Path) -> dict[str, str]:
    """Opaque artifact id -> protected report-relative screenshot path."""
    report, _ = _load_report(run_dir)
    if report is None:
        return {}
    refs: dict[str, str] = {}
    for index, result in enumerate(report.results, start=1):
        values = {
            f"step-{index:03d}-before": result.before_png,
            f"step-{index:03d}-after": result.after_png,
            f"step-{index:03d}-heal": (result.heal.screenshot if result.heal else None),
        }
        for artifact_id, value in values.items():
            if isinstance(value, str) and value:
                refs[artifact_id] = value
    return refs


def _is_png(path: Path) -> bool:
    """Console artifacts are runtime screenshots, which Flow writes as PNG."""
    try:
        with path.open("rb") as fh:
            return fh.read(8) == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False


def safe_artifact(run_dir: Path, artifact_id: str) -> Optional[Path]:
    """Resolve a report-referenced PNG without traversal or symlink following."""
    rel = _report_image_refs(run_dir).get(artifact_id, "")
    if not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
        return None
    if Path(rel).suffix.lower() != ".png":
        return None
    root = run_dir.resolve(strict=True)
    lexical = run_dir / rel
    # Reject a symlink in any path component, including the file itself.
    current = run_dir
    for part in Path(rel).parts:
        current = current / part
        if current.is_symlink():
            return None
    try:
        candidate = lexical.resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() and _is_png(candidate) else None


# ---------------------------------------------------------------------------
# skill library projections
# ---------------------------------------------------------------------------


def find_skill_libraries(*roots: Optional[Path]) -> list[Path]:
    """Every ``skills.json`` under the given roots (depth-limited), i.e. the
    versioned skill libraries ``teach`` maintains next to updated bundles."""
    seen: set[Path] = set()
    out: list[Path] = []
    for root in roots:
        if root is None or not root.is_dir():
            continue
        root = root.resolve()
        frontier = [root]
        for _ in range(_SCAN_DEPTH + 1):
            nxt: list[Path] = []
            for directory in frontier:
                candidate = directory / "skills.json"
                if candidate.is_file() and not candidate.is_symlink():
                    try:
                        resolved = candidate.resolve(strict=True)
                        resolved.relative_to(root)
                    except (OSError, ValueError):
                        pass
                    else:
                        if resolved not in seen:
                            seen.add(resolved)
                            out.append(resolved)
                try:
                    nxt.extend(
                        child
                        for child in sorted(directory.iterdir())
                        if not child.is_symlink() and child.is_dir()
                    )
                except OSError:
                    continue
            frontier = nxt
    return out


def skill_library_id(library_file: Path) -> str:
    """Opaque stable id; never expose the absolute local library path."""
    return hashlib.sha256(str(library_file.resolve()).encode("utf-8")).hexdigest()[:24]


def skill_public_id(library_file: Path, skill_id: str) -> str:
    material = f"{skill_library_id(library_file)}\0{skill_id}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:24]


def resolve_skill_id(library_file: Path, public_id: str) -> Optional[str]:
    from openadapt_flow.learning.library import SkillLibrary

    try:
        library = SkillLibrary(library_file.parent)
    except Exception:  # noqa: BLE001 - caller returns a bounded 404
        return None
    for skill_id in library.skill_ids():
        if skill_public_id(library_file, skill_id) == public_id:
            return skill_id
    return None


def skill_library_view(library_file: Path) -> dict[str, Any]:
    """Versions + lineage of every skill in one library, WITHOUT the (large)
    program graphs -- the governance metadata only."""
    from openadapt_flow.learning.library import SkillLibrary

    try:
        lib = SkillLibrary(library_file.parent)
    except Exception as e:  # noqa: BLE001
        return {
            "id": skill_library_id(library_file),
            "error": f"{type(e).__name__}: skill library could not be loaded safely",
        }
    skills = []
    for sid in lib.skill_ids():
        skill = lib.get(sid)
        skills.append(
            {
                "id": skill_public_id(library_file, sid),
                "versions": [
                    {
                        "version": v.version,
                        "status": (
                            v.status
                            if v.status
                            in {
                                "active",
                                "candidate",
                                "superseded",
                                "rolled_back",
                                "quarantined",
                            }
                            else "unknown"
                        ),
                        "validation_score": v.validation_score,
                        "created_at": _safe_timestamp(v.provenance.created_at),
                        "parent_version": v.provenance.parent_version,
                        "n_traces": len(v.provenance.trace_ids),
                    }
                    for v in skill.versions
                ],
            }
        )
    return {"id": skill_library_id(library_file), "skills": skills}
