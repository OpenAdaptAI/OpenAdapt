"""Export an immutable public-demo pack from real local Flow artifacts.

The target is the bundled, first-party MockMed application. All target data is
synthetic, but no OpenAdapt result is simulated: this script records the app
through :class:`Recorder`, compiles that recording, projects the compiled
program graph, runs the Standard profile against an independent system-of-
record read-back, executes the required representative and fault cases, and
certifies the resulting qualification project.

The exporter is deliberately localhost-only and headless. It never configures a
grounder, identity model, or external service. Healthy runs therefore prove
``model_calls == 0`` through the real :class:`RunReport`; fault outcomes come
from the same runtime rather than from authored UI-state labels.

Usage::

    python scripts/export_public_demo_evidence.py \
      --out public-demo/evidence-packs \
      --pack-id mockmed-triage-v1

The pack directory is created atomically and is never overwritten. Run
``--validate <pack-dir>`` to re-check every retained byte, crop binding, case
outcome, and aggregate.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Optional
from urllib.request import Request, urlopen

from jsonschema import Draft202012Validator
from PIL import Image

from openadapt_flow import __version__ as FLOW_VERSION
from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.compiler import compile_recording
from openadapt_flow.deployment import DeploymentConfig, PolicySection
from openadapt_flow.execution_profiles import (
    ExecutionProfile,
    execution_profile_contract,
)
from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.mockmed.fault_server import serve
from openadapt_flow.policy import has_structured_identity, load_policy
from openadapt_flow.qualification import (
    ActionRiskClass,
    ActionRiskClassification,
    EnvironmentBoundary,
    EvidenceRef,
    IdentityEnforcement,
    IdentityPolicy,
    QualificationCase,
    QualificationCaseKind,
    QualificationCaseResult,
    QualificationOutcome,
    add_case,
    certify_project,
    init_project,
    record_case_results,
    save_qualified_workflow,
    set_action_classification,
    set_effect_policy,
    set_identity_policy,
    set_trusted_runner_key,
    sign_case_result,
    workflow_contract_sha256,
)
from openadapt_flow.recorder import Recorder
from openadapt_flow.report import render_run_report
from openadapt_flow.run_gate import (
    build_runtime_authorization,
    evaluate_run_gate,
)
from openadapt_flow.runtime import Replayer
from openadapt_flow.runtime.effects import RestRecordVerifier
from openadapt_flow.verification import VerificationTier
from openadapt_flow.visualize import build_program_graph, render_html

SCHEMA_VERSION = "openadapt.public-demo-evidence/v1"
OUTCOME_SCHEMA_VERSION = "openadapt.public-demo-outcome/v1"
PACK_VERSION = 1
TRIALS_PER_CASE = 3
NOTE = "Synthetic follow-up in two weeks"
WORKFLOW_NAME = "mockmed-triage"
RUNNER_KEY_ID = "public-demo-headless-runner"
NON_PUBLIC_RUN_AUTHORITY = frozenset(
    {
        ".attended_action.lease",
        ".attended_capability.key",
        ".attended_program_receipts",
        "approval.json",
        "approval.json.enc",
        "attended_capability.json",
        "attended_capability_history.json",
        "attended_decisions.json",
    }
)
REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "schemas" / "public-demo-evidence-v1.json"


class EvidencePackError(RuntimeError):
    """The evidence pack could not be generated or validated safely."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _source_provenance(*, allow_dirty: bool) -> dict[str, Any]:
    commit = _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--porcelain", "--untracked-files=normal"))
    if dirty and not allow_dirty:
        raise EvidencePackError(
            "source worktree is dirty; commit the exporter/app source before "
            "creating immutable public evidence (or use --allow-dirty only for "
            "local development)"
        )
    return {
        "repository": "https://github.com/OpenAdaptAI/openadapt-flow",
        "source_commit": commit,
        "source_tree_clean": not dirty,
        "exporter": "scripts/export_public_demo_evidence.py",
        "openadapt_flow_version": FLOW_VERSION,
        "license": "MIT",
        "data_classification": "synthetic_sample",
    }


def _tree_digest(root: Path) -> str:
    rows = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
        )
    return _sha256_bytes(_canonical_json(rows))


def _is_pack_control(root: Path, path: Path) -> bool:
    return path.relative_to(root).as_posix() in {"manifest.json", "manifest.sha256"}


def _http_json(url: str, *, method: str = "GET", body: Any = None) -> Any:
    data = _canonical_json(body) if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=5) as response:  # noqa: S310 - loopback only
        if response.status // 100 != 2:
            raise EvidencePackError(f"loopback request failed: {response.status}")
        return json.loads(response.read().decode("utf-8"))


def _records_reader(base_url: str) -> Callable[[], list[dict[str, Any]]]:
    def read() -> list[dict[str, Any]]:
        payload = _http_json(f"{base_url}api/db")
        records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(records, list) or not all(
            isinstance(item, dict) for item in records
        ):
            raise EvidencePackError("MockMed /api/db returned malformed records")
        return records

    return read


def _center(page: Any, selector: str) -> tuple[int, int]:
    locator = page.locator(selector).first
    locator.wait_for(state="visible")
    box = locator.bounding_box()
    if box is None:
        raise EvidencePackError(f"record target {selector!r} has no bounding box")
    return (
        int(box["x"] + box["width"] / 2),
        int(box["y"] + box["height"] / 2),
    )


def _finish_video(video_dir: Path, target: Path) -> Path:
    videos = sorted(video_dir.glob("*.webm"))
    if len(videos) != 1 or videos[0].stat().st_size <= 0:
        raise EvidencePackError(
            f"expected exactly one non-empty Playwright video in {video_dir}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    videos[0].replace(target)
    video_dir.rmdir()
    return target


def _record(
    base_url: str,
    recording_dir: Path,
    media_dir: Path,
) -> dict[str, Any]:
    """Drive the real Recorder with a read-only source-of-record observer."""

    _http_json(f"{base_url}api/reset", method="POST", body={})
    video_tmp = media_dir / ".recording-video"
    video_tmp.mkdir(parents=True)
    entry_url = f"{base_url}?fault=ok&idempotency=demo#tasks"
    backend, close = PlaywrightBackend.launch(
        entry_url,
        headless=True,
        record_video_dir=str(video_tmp),
        system_of_record_reader=_records_reader(base_url),
    )
    environment: dict[str, Any] = {}
    try:
        page = backend.page
        environment = {
            "browser": "chromium",
            "browser_version": page.context.browser.version,
            "user_agent": page.evaluate("navigator.userAgent"),
            "viewport": list(backend.viewport),
            "device_scale_factor": 1,
            "headless": True,
            "platform": platform.platform(),
            "python": platform.python_version(),
        }
        recorder = Recorder(backend, recording_dir, app_url=entry_url)
        recorder.click(*_center(page, ".open-btn"))
        recorder.click(*_center(page, "#new-encounter"))
        recorder.click(*_center(page, "#type-triage"))
        recorder.click(*_center(page, "#note-label"))
        recorder.type_text(NOTE, param="note")
        recorder.click(*_center(page, "#save-encounter"))
        page.wait_for_selector("#saved-banner", state="visible")
        page.wait_for_timeout(250)
        recorder.finish()
    finally:
        close()
    _finish_video(video_tmp, media_dir / "recording.webm")
    return environment


def _save_step(workflow: Workflow) -> Any:
    candidates = [
        step for step in workflow.steps if step.risk == "irreversible" and step.effects
    ]
    if len(candidates) != 1:
        raise EvidencePackError(
            "expected the compiler to derive exactly one consequential "
            f"effect-bound step, observed {[step.id for step in candidates]}"
        )
    step = candidates[0]
    if (
        step.anchor is None
        or not step.identity_armed
        or not has_structured_identity(step)
    ):
        raise EvidencePackError(
            "consequential save did not compile with retained structured "
            "identity evidence"
        )
    if any(effect.needs_operator_confirmation for effect in step.effects):
        raise EvidencePackError("compiler emitted an unbound placeholder effect")
    return step


def _configure_qualification(
    workflow: Workflow,
    *,
    environment: dict[str, Any],
    app_digest: str,
) -> tuple[bytes, str]:
    environment_payload = {
        **environment,
        "application_sha256": app_digest,
        "runtime_version": FLOW_VERSION,
        "target_kind": "web",
    }
    environment_digest = _sha256_bytes(_canonical_json(environment_payload))
    boundary = EnvironmentBoundary(
        target_kind="web",
        application="OpenAdapt MockMed synthetic reference",
        application_version=f"sha256:{app_digest}",
        environment_digest=environment_digest,
        runtime_version=FLOW_VERSION,
        required_capabilities=[
            "headless_chromium",
            "independent_system_of_record",
            "playwright_dom",
        ],
    )
    project = init_project(
        workflow,
        environment=boundary,
        minimum_effect_tier=VerificationTier.INDEPENDENT_SYSTEM,
    )

    for step in workflow.steps:
        classification = (
            ActionRiskClass.IRREVERSIBLE
            if step.risk == "irreversible"
            else ActionRiskClass.READ_ONLY
        )
        explanation = (
            "compiler classified this as an irreversible system-of-record write"
            if classification is ActionRiskClass.IRREVERSIBLE
            else "reviewed as workflow preparation/navigation with no business-record effect"
        )
        set_action_classification(
            workflow,
            ActionRiskClassification(
                step_id=step.id,
                classification=classification,
                explanation=explanation,
                operator_confirmed=True,
            ),
        )

    save = _save_step(workflow)
    set_identity_policy(
        workflow,
        IdentityPolicy(
            step_id=save.id,
            enforcement=IdentityEnforcement.CANONICAL_LADDER,
        ),
    )
    for index, _effect in enumerate(save.effects):
        set_effect_policy(
            workflow,
            step_id=save.id,
            effect_index=index,
            tier=VerificationTier.INDEPENDENT_SYSTEM,
        )
    add_case(
        workflow,
        QualificationCase(
            id="representative",
            kind=QualificationCaseKind.REPRESENTATIVE,
            expected_outcome=QualificationOutcome.VERIFIED,
            description="Recorded workflow under its qualified application boundary",
        ),
    )

    # This key is pack-local, generated for this immutable campaign, and never
    # reused as a production trust root.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    set_trusted_runner_key(
        workflow,
        key_id=RUNNER_KEY_ID,
        public_key_base64=base64.b64encode(public_raw).decode("ascii"),
    )
    assert workflow.qualification is project
    return private_raw, environment_digest


class _StaleIdentityBackend(PlaywrightBackend):
    """Change the live record identity after the first identity observation."""

    def __init__(self, page: Any) -> None:
        super().__init__(page)
        self._changed = False

    def structured_text_at(self, x: int, y: int) -> Optional[str]:
        observed = super().structured_text_at(x, y)
        if observed and "Jane Sample" in observed and not self._changed:
            self._changed = True
            self.page.evaluate(
                """() => {
                    const row = document.querySelector('#open-p1').closest('tr');
                    row.children[0].textContent = 'Changed Identity';
                }"""
            )
        return observed


def _origin(url: str) -> str:
    return url.rstrip("/").split("?", 1)[0]


def _case_plan() -> list[dict[str, Any]]:
    """Machine conditions keyed to the canonical qualification case ids."""

    return [
        {
            "case_id": "representative",
            "query": "?fault=ok&idempotency=demo",
            "expected": "VERIFIED",
            "oracle": "exact_record",
            "use_structural": True,
        },
        {
            "case_id": "fault-ambiguity",
            "query": "?fault=ok&drift=ambiguous&idempotency=demo",
            "expected": "HALTED",
            "oracle": "no_mutation",
            "use_structural": True,
        },
        {
            "case_id": "fault-wrong-identity",
            "query": "?fault=ok&drift=lookalike&idempotency=demo",
            "expected": "HALTED",
            "oracle": "no_mutation",
            "use_structural": False,
        },
        {
            "case_id": "fault-stale-identity",
            "query": "?fault=ok&idempotency=demo",
            "expected": "HALTED",
            "oracle": "no_mutation",
            "use_structural": True,
            "backend": "stale_identity",
        },
        {
            "case_id": "fault-weak-effect",
            "query": "?fault=partial&idempotency=demo",
            "expected": "HALTED",
            "oracle": "partial_write_detected",
            "use_structural": True,
        },
        {
            "case_id": "fault-missing-effect",
            "query": "?fault=optimistic&idempotency=demo",
            "expected": "HALTED",
            "oracle": "rejected_write_detected",
            "use_structural": True,
        },
    ]


def _oracle_snapshot(
    base_url: str,
    *,
    oracle_kind: str,
    report: RunReport,
) -> dict[str, Any]:
    snapshot = _http_json(f"{base_url}api/db")
    records = snapshot.get("records", [])
    rejected = snapshot.get("rejected_writes", 0)
    exact = [
        record
        for record in records
        if record.get("patient_id") == "p1"
        and record.get("type") == "Triage"
        and record.get("note") == NOTE
    ]
    wrong_target = any(record.get("patient_id") not in {"p1"} for record in records)
    if oracle_kind == "exact_record":
        passed = len(records) == 1 and len(exact) == 1
        observed = "exact_record"
    elif oracle_kind == "no_mutation":
        passed = not records and not rejected
        observed = "no_mutation" if passed else "unexpected_mutation"
    elif oracle_kind == "partial_write_detected":
        partial = [
            record
            for record in records
            if record.get("patient_id") == "p1"
            and record.get("type") == "Triage"
            and record.get("note") == ""
        ]
        passed = len(records) == 1 and len(partial) == 1
        observed = "partial_write" if passed else "partial_write_not_observed"
    elif oracle_kind == "rejected_write_detected":
        passed = not records and rejected == 1
        observed = "rejected_write" if passed else "rejection_not_observed"
    else:
        raise EvidencePackError(f"unknown oracle kind {oracle_kind!r}")
    return {
        "schema_version": "openadapt.public-demo-oracle/v1",
        "oracle_kind": oracle_kind,
        "read_path": "GET /api/db",
        "read_boundary": "independent from browser pixels and RunReport",
        "passed": passed,
        "observed": observed,
        "wrong_target_action": wrong_target,
        "silent_incorrect_success": bool(not passed and report.success),
        "snapshot": snapshot,
    }


def _report_events(report: RunReport) -> str:
    rows = []
    for index, result in enumerate(report.results):
        rows.append(
            json.dumps(
                {
                    "schema_version": "openadapt.public-demo-run-event/v1",
                    "index": index,
                    **result.model_dump(mode="json"),
                },
                sort_keys=True,
            )
        )
    return "\n".join(rows) + ("\n" if rows else "")


def _outcome_envelope(
    *,
    case_id: str,
    trial: int,
    expected_outcome: str,
    report: RunReport,
    report_path: Path,
    oracle: dict[str, Any],
) -> dict[str, Any]:
    if report.execution_outcome is None:
        raise EvidencePackError("run report has no precise execution outcome")
    failed = next((item for item in report.results if not item.ok), None)
    return {
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "case_id": case_id,
        "trial": trial,
        "expected_outcome": expected_outcome,
        "observed_outcome": report.execution_outcome,
        "matched_expectation": report.execution_outcome == expected_outcome,
        "execution_profile": report.execution_profile,
        "production_eligible": report.production_eligible,
        "execution_completed": report.execution_completed,
        "report_sha256": _sha256(report_path),
        "model_calls": report.model_calls,
        "external_network_calls": 0,
        "screenshots_may_leave_box": report.screenshots_may_leave_box,
        "duration_ms": report.total_ms,
        "wrong_target_action": oracle["wrong_target_action"],
        "silent_incorrect_success": oracle["silent_incorrect_success"],
        "oracle_passed": oracle["passed"],
        "failed_step_id": failed.step_id if failed is not None else None,
        "halt": report.halt.model_dump(mode="json") if report.halt else None,
    }


def _strip_run_authority(run_dir: Path) -> None:
    """Remove live resume/approval authority from a public evidence derivative."""
    for name in NON_PUBLIC_RUN_AUTHORITY:
        path = run_dir / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def _reject_run_authority(relative: str) -> None:
    parts = PurePosixPath(relative).parts
    secret_like = any(
        part.lower().endswith(".key")
        or any(marker in part.lower() for marker in ("credential", "secret", "token"))
        for part in parts
    )
    if NON_PUBLIC_RUN_AUTHORITY.intersection(parts) or secret_like:
        raise EvidencePackError(
            f"public evidence must not contain live run authority: {relative}"
        )


def _run_case_trial(
    *,
    base_url: str,
    workflow: Workflow,
    bundle_dir: Path,
    case: dict[str, Any],
    trial: int,
    case_dir: Path,
) -> tuple[RunReport, dict[str, Any], dict[str, Any]]:
    _http_json(f"{base_url}api/reset", method="POST", body={})
    trial_dir = case_dir / f"trial-{trial:02d}"
    run_dir = trial_dir / "run"
    media_tmp = trial_dir / ".video"
    record_video = trial == 1
    if record_video:
        media_tmp.mkdir(parents=True)
    entry_url = f"{base_url.rstrip('/')}/{case['query']}#tasks"
    backend, close = PlaywrightBackend.launch(
        entry_url,
        headless=True,
        record_video_dir=str(media_tmp) if record_video else None,
    )
    active_backend: PlaywrightBackend = backend
    if case.get("backend") == "stale_identity":
        active_backend = _StaleIdentityBackend(backend.page)
    try:
        verifier = RestRecordVerifier(
            base_url,
            records_path="/api/db",
            records_key="records",
            timeout_s=1.0,
            poll_interval_s=0.05,
        )
        gate = evaluate_run_gate(
            workflow,
            bundle_dir=bundle_dir,
            deployment=DeploymentConfig(policy=PolicySection(policy="clinical-write")),
            effect_verifier=verifier,
            profile_contract=execution_profile_contract(ExecutionProfile.STANDARD),
            effective_durable=True,
            effective_require_settled=True,
        )
        if not gate.passed:
            raise EvidencePackError(gate.render())
        authorization = build_runtime_authorization(
            workflow,
            gate,
            approval_source="public-demo-qualified-campaign",
            params={"note": NOTE},
        )
        report = Replayer(
            active_backend,
            effect_verifier=verifier,
            governed_authorization=authorization,
            durable=True,
            require_settled=True,
            use_structural=bool(case["use_structural"]),
        ).run(
            workflow.model_copy(deep=True),
            params={"note": NOTE},
            bundle_dir=bundle_dir,
            run_dir=run_dir,
            execution_target_kind="web",
            execution_origin=_origin(entry_url),
            execution_entry_url=entry_url,
        )
        backend.page.wait_for_timeout(200)
    finally:
        close()
    if record_video:
        _finish_video(
            media_tmp,
            trial_dir
            / ("replay.webm" if case["expected"] == "VERIFIED" else "halt.webm"),
        )

    report_path = run_dir / "report.json"
    render_run_report(run_dir)
    _strip_run_authority(run_dir)
    oracle = _oracle_snapshot(
        base_url,
        oracle_kind=str(case["oracle"]),
        report=report,
    )
    _write_json(trial_dir / "oracle.json", oracle)
    (trial_dir / "events.jsonl").write_text(
        _report_events(report),
        encoding="utf-8",
    )
    envelope = _outcome_envelope(
        case_id=str(case["case_id"]),
        trial=trial,
        expected_outcome=str(case["expected"]),
        report=report,
        report_path=report_path,
        oracle=oracle,
    )
    _write_json(trial_dir / "outcome.json", envelope)
    if report.execution_outcome != case["expected"]:
        raise EvidencePackError(
            f"{case['case_id']} trial {trial} observed "
            f"{report.execution_outcome}, expected {case['expected']}"
        )
    if report.model_calls != 0 or report.screenshots_may_leave_box:
        raise EvidencePackError(
            f"{case['case_id']} trial {trial} violated zero-model/local boundary"
        )
    if not oracle["passed"] or oracle["silent_incorrect_success"]:
        raise EvidencePackError(
            f"{case['case_id']} trial {trial} failed independent oracle"
        )
    return report, oracle, envelope


def _evidence_ref(root: Path, path: Path, kind: str) -> EvidenceRef:
    return EvidenceRef(
        kind=kind,
        sha256=_sha256(path),
        relative_path=path.relative_to(root).as_posix(),
    )


def _case_result(
    *,
    root: Path,
    workflow: Workflow,
    case_id: str,
    observed_outcome: str,
    report_paths: Iterable[Path],
    oracle_paths: Iterable[Path],
    private_key: bytes,
) -> QualificationCaseResult:
    project = workflow.qualification
    if project is None:
        raise EvidencePackError("workflow qualification project disappeared")
    result = QualificationCaseResult(
        case_id=case_id,
        project_id=project.project_id,
        project_revision=project.revision,
        project_contract_sha256=project.contract_sha256(),
        workflow_contract_sha256=workflow_contract_sha256(workflow),
        environment_contract_sha256=project.environment.contract_sha256(),
        environment_digest=project.environment.environment_digest,
        runtime_version=project.environment.runtime_version,
        runner_id="openadapt-flow/public-demo-headless",
        runner_capabilities=list(project.environment.required_capabilities),
        status="passed",
        observed_outcome=QualificationOutcome(observed_outcome.lower()),
        evidence=[
            *(_evidence_ref(root, path, "run_report") for path in report_paths),
            *(_evidence_ref(root, path, "effect") for path in oracle_paths),
        ],
        detail_code=f"{case_id}.three-trial-contract",
        attestation_key_id=RUNNER_KEY_ID,
    )
    return sign_case_result(result, private_key=private_key)


def _copy_binding(
    *,
    root: Path,
    workflow: Workflow,
    recording_dir: Path,
    bundle_dir: Path,
) -> list[dict[str, Any]]:
    bindings: list[dict[str, Any]] = []
    for step in workflow.steps:
        if step.anchor is None:
            continue
        try:
            event_index = int(step.id.removeprefix("step_"))
        except ValueError as exc:
            raise EvidencePackError(
                f"non-canonical compiled step id {step.id!r}"
            ) from exc
        source = recording_dir / "frames" / f"{event_index:04d}_before.png"
        crop = bundle_dir / step.anchor.template
        if not source.is_file() or not crop.is_file():
            raise EvidencePackError(f"missing crop source for {step.id}")
        with Image.open(source) as raw_image, Image.open(crop) as crop_image:
            x, y, width, height = step.anchor.region
            expected = raw_image.convert("RGB").crop((x, y, x + width, y + height))
            actual = crop_image.convert("RGB")
            if expected.size != actual.size or expected.tobytes() != actual.tobytes():
                raise EvidencePackError(
                    f"compiled template pixels do not match raw frame region for {step.id}"
                )
        bindings.append(
            {
                "step_id": step.id,
                "crop_path": crop.relative_to(root).as_posix(),
                "crop_sha256": _sha256(crop),
                "source_frame_path": source.relative_to(root).as_posix(),
                "source_frame_sha256": _sha256(source),
                "region": list(step.anchor.region),
            }
        )
        if step.anchor.identifier_crop:
            identity_crop = bundle_dir / step.anchor.identifier_crop
            if not identity_crop.is_file() or step.anchor.identifier_region is None:
                raise EvidencePackError(
                    f"identifier crop binding is incomplete for {step.id}"
                )
            bindings.append(
                {
                    "step_id": step.id,
                    "crop_path": identity_crop.relative_to(root).as_posix(),
                    "crop_sha256": _sha256(identity_crop),
                    "source_frame_path": source.relative_to(root).as_posix(),
                    "source_frame_sha256": _sha256(source),
                    "region": list(step.anchor.identifier_region),
                }
            )
    return bindings


def _media_type(path: Path) -> str:
    return {
        ".json": "application/json",
        ".jsonl": "application/x-ndjson",
        ".md": "text/markdown",
        ".html": "text/html",
        ".py": "text/x-python",
        ".png": "image/png",
        ".webm": "video/webm",
    }.get(path.suffix.lower(), "application/octet-stream")


def _role(path: str) -> str:
    if "/recording/" in path:
        return "source_recording"
    if "/bundle/" in path:
        return "compiled_bundle"
    if "/qualification/" in path:
        return "qualification"
    if path.endswith(".webm"):
        return "media"
    if "/cases/" in path:
        return "case_evidence"
    if "/program-graph." in path:
        return "program_graph"
    return "evidence"


def _file_ref(root: Path, path: Path) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    return {
        "path": relative,
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
        "role": _role(relative),
        "media_type": _media_type(path),
    }


def _ref_for(root: Path, path: Path) -> dict[str, Any]:
    return _file_ref(root, path)


def _poster_for_run(run_dir: Path, report: RunReport) -> Optional[Path]:
    candidate_result = next(
        (result for result in reversed(report.results) if result.after_png),
        None,
    )
    if candidate_result is None or candidate_result.after_png is None:
        return None
    candidate = run_dir / candidate_result.after_png
    return candidate if candidate.is_file() else None


def _assemble_manifest(
    *,
    root: Path,
    pack_id: str,
    provenance: dict[str, Any],
    environment: dict[str, Any],
    environment_digest: str,
    app_digest: str,
    workflow: Workflow,
    qualification_report: dict[str, Any],
    cases: list[dict[str, Any]],
    crop_bindings: list[dict[str, Any]],
    trials: int,
) -> dict[str, Any]:
    payload_files = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and not _is_pack_control(root, path)
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in payload_files:
        _reject_run_authority(path.relative_to(root).as_posix())
    files = [_file_ref(root, path) for path in payload_files]
    outcomes = Counter(
        envelope["observed_outcome"] for case in cases for envelope in case["outcomes"]
    )
    reports = [
        RunReport.model_validate_json((root / ref["path"]).read_text(encoding="utf-8"))
        for case in cases
        for ref in case["reports"]
    ]
    oracles = [
        json.loads((root / ref["path"]).read_text(encoding="utf-8"))
        for case in cases
        for ref in case["oracles"]
    ]
    project = workflow.qualification
    assert project is not None
    graph_json = root / "artifacts" / "compiled" / "program-graph.json"
    graph_html = root / "artifacts" / "compiled" / "program-graph.html"
    recording = root / "artifacts" / "recording"
    bundle = root / "artifacts" / "bundle"
    qualification = root / "artifacts" / "qualification"
    return {
        "schema_version": SCHEMA_VERSION,
        "pack": {
            "id": pack_id,
            "version": PACK_VERSION,
            "generated_at": _now(),
            "immutable": True,
        },
        "provenance": {
            **provenance,
            "application": {
                "name": project.environment.application,
                "version": project.environment.application_version,
                "source_path": "openadapt_flow/mockmed",
                "source_sha256": app_digest,
                "license": "MIT",
                "synthetic_data_only": True,
            },
            "runtime": environment,
        },
        "task": {
            "workflow_name": workflow.name,
            "target_kind": project.environment.target_kind,
            "parameter_names": sorted(workflow.params),
            "program_graph_ref": _ref_for(root, graph_json),
        },
        "evaluation": {
            "environment_digest": environment_digest,
            "trials_per_case": trials,
            "case_count": len(cases),
            "run_count": len(reports),
            "required_case_kinds": sorted(
                case.kind.value for case in project.cases if case.required
            ),
            "outcome_counts": dict(sorted(outcomes.items())),
            "model_calls": sum(report.model_calls for report in reports),
            "external_network_calls": 0,
            "screenshots_may_leave_box": any(
                report.screenshots_may_leave_box for report in reports
            ),
            "wrong_target_actions": sum(
                int(oracle["wrong_target_action"]) for oracle in oracles
            ),
            "silent_incorrect_successes": sum(
                int(oracle["silent_incorrect_success"]) for oracle in oracles
            ),
            "over_halts": 0,
            "total_duration_ms": sum(report.total_ms for report in reports),
            "oracle": {
                "kind": "independent_system_of_record",
                "verifier": ("openadapt_flow.runtime.effects.rest.RestRecordVerifier"),
                "verification_tier": int(VerificationTier.INDEPENDENT_SYSTEM),
                "summary": (
                    "GET /api/db is independent of browser pixels and the "
                    "runtime report; target state is held in the local MockMed "
                    "fault server."
                ),
            },
            "required_contracts": qualification_report["case_count"],
            "passed_contracts": qualification_report["passed_case_count"],
            "minimum_effect_tier": int(project.minimum_effect_tier),
            "qualification_passed": qualification_report["passed"],
            "caveats": [
                "First-party synthetic MockMed task; not customer production evidence.",
                "Bound to the exact app, browser, viewport, runtime, and source commit in this pack.",
                "Loopback HTTP is used for the local app and independent read-back; no external service is contacted.",
                "Three trials per required condition establish this bounded campaign only.",
                "The synthetic reference app exposes a stable demo-mode idempotency key so the compiler can retain and verify a real at-most-once contract.",
            ],
        },
        "artifacts": {
            "source_recording": {
                "meta": _ref_for(root, recording / "meta.json"),
                "events": _ref_for(root, recording / "events.jsonl"),
                "media": _ref_for(
                    root, root / "artifacts" / "media" / "recording.webm"
                ),
                "frame_count": len(list((recording / "frames").glob("*.png"))),
            },
            "compiled": {
                "workflow": _ref_for(root, bundle / "workflow.json"),
                "workflow_source": _ref_for(root, bundle / "workflow.py"),
                "content_digest": workflow.manifest.content_digest
                if workflow.manifest
                else None,
                "workflow_contract_sha256": workflow_contract_sha256(workflow),
                "program_graph": _ref_for(root, graph_json),
                "program_graph_html": _ref_for(root, graph_html),
            },
            "qualification": {
                "project": _ref_for(root, qualification / "project.json"),
                "report": _ref_for(root, qualification / "report.json"),
                "passed": qualification_report["passed"],
                "minimum_effect_tier": int(project.minimum_effect_tier),
            },
            "cases": cases,
            "crop_bindings": crop_bindings,
        },
        "files": files,
    }


def _case_manifest(
    *,
    root: Path,
    workflow: Workflow,
    case_config: dict[str, Any],
    reports: list[RunReport],
    case_dir: Path,
) -> dict[str, Any]:
    project = workflow.qualification
    assert project is not None
    qualification_case = next(
        case for case in project.cases if case.id == case_config["case_id"]
    )
    report_refs = []
    outcome_refs = []
    oracle_refs = []
    event_refs = []
    outcomes = []
    for index, report in enumerate(reports, start=1):
        trial_dir = case_dir / f"trial-{index:02d}"
        report_refs.append(_ref_for(root, trial_dir / "run" / "report.json"))
        outcome_path = trial_dir / "outcome.json"
        outcome_refs.append(_ref_for(root, outcome_path))
        oracle_refs.append(_ref_for(root, trial_dir / "oracle.json"))
        event_refs.append(_ref_for(root, trial_dir / "events.jsonl"))
        outcomes.append(json.loads(outcome_path.read_text(encoding="utf-8")))
    first_trial = case_dir / "trial-01"
    media_name = "replay.webm" if case_config["expected"] == "VERIFIED" else "halt.webm"
    poster = _poster_for_run(first_trial / "run", reports[0])
    return {
        "case_id": qualification_case.id,
        "kind": qualification_case.kind.value,
        "expected_outcome": qualification_case.expected_outcome.value.upper(),
        "reports": report_refs,
        "outcome_envelopes": outcome_refs,
        "outcomes": outcomes,
        "oracles": oracle_refs,
        "events": event_refs,
        "media": _ref_for(root, first_trial / media_name),
        "poster": _ref_for(root, poster) if poster is not None else None,
    }


def export_pack(
    *,
    output_root: Path,
    pack_id: str,
    trials: int,
    allow_dirty: bool = False,
) -> Path:
    if trials < 3:
        raise EvidencePackError("public evidence requires at least three trials/case")
    if not pack_id or any(
        char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in pack_id
    ):
        raise EvidencePackError(
            "pack id must use lowercase letters, digits, and hyphens"
        )
    output_root = output_root.resolve()
    destination = output_root / pack_id
    if destination.exists():
        raise EvidencePackError(
            f"immutable pack already exists: {destination}; choose a new pack id"
        )
    output_root.mkdir(parents=True, exist_ok=True)
    provenance = _source_provenance(allow_dirty=allow_dirty)
    temp_root = Path(tempfile.mkdtemp(prefix=f".{pack_id}.", dir=str(output_root)))
    base_url = ""
    stop: Optional[Callable[[], None]] = None
    try:
        artifacts = temp_root / "artifacts"
        recording_dir = artifacts / "recording"
        bundle_dir = artifacts / "bundle"
        media_dir = artifacts / "media"
        qualification_dir = artifacts / "qualification"
        media_dir.mkdir(parents=True)
        base_url, _db, stop = serve()

        environment = _record(base_url, recording_dir, media_dir)
        workflow = compile_recording(
            recording_dir,
            bundle_dir,
            name=WORKFLOW_NAME,
            mine_effects=True,
        )
        save = _save_step(workflow)
        if not any(
            effect.kind.value == "record_written" for effect in save.effects
        ) or not any(effect.kind.value == "field_equals" for effect in save.effects):
            raise EvidencePackError(
                "recording did not compile the required source-of-record effects"
            )
        app_digest = _tree_digest(REPO_ROOT / "openadapt_flow" / "mockmed")
        private_key, environment_digest = _configure_qualification(
            workflow,
            environment=environment,
            app_digest=app_digest,
        )
        save_qualified_workflow(workflow, bundle_dir)
        workflow = Workflow.load(bundle_dir)

        case_manifests: list[dict[str, Any]] = []
        qualification_results: list[QualificationCaseResult] = []
        for case in _case_plan():
            case_dir = artifacts / "cases" / str(case["case_id"])
            reports: list[RunReport] = []
            report_paths: list[Path] = []
            oracle_paths: list[Path] = []
            for trial in range(1, trials + 1):
                report, _oracle, _envelope = _run_case_trial(
                    base_url=base_url,
                    workflow=workflow,
                    bundle_dir=bundle_dir,
                    case=case,
                    trial=trial,
                    case_dir=case_dir,
                )
                reports.append(report)
                trial_dir = case_dir / f"trial-{trial:02d}"
                report_paths.append(trial_dir / "run" / "report.json")
                oracle_paths.append(trial_dir / "oracle.json")
            qualification_results.append(
                _case_result(
                    root=temp_root,
                    workflow=workflow,
                    case_id=str(case["case_id"]),
                    observed_outcome=str(case["expected"]),
                    report_paths=report_paths,
                    oracle_paths=oracle_paths,
                    private_key=private_key,
                )
            )
            case_manifests.append(
                _case_manifest(
                    root=temp_root,
                    workflow=workflow,
                    case_config=case,
                    reports=reports,
                    case_dir=case_dir,
                )
            )

        record_case_results(
            workflow,
            qualification_results,
            evidence_root=temp_root,
        )
        qualification_report_model = certify_project(
            workflow,
            policy=load_policy("clinical-write"),
            evidence_root=temp_root,
        )
        if not qualification_report_model.passed:
            raise EvidencePackError(qualification_report_model.render())
        qualification_report = qualification_report_model.model_dump(mode="json")
        _write_json(
            qualification_dir / "project.json",
            workflow.qualification.model_dump(mode="json")
            if workflow.qualification
            else {},
        )
        _write_json(qualification_dir / "report.json", qualification_report)
        save_qualified_workflow(workflow, bundle_dir)
        workflow = Workflow.load(bundle_dir)

        compiled_dir = artifacts / "compiled"
        graph = build_program_graph(workflow)
        _write_json(
            compiled_dir / "program-graph.json",
            graph.model_dump(mode="json"),
        )
        (compiled_dir / "program-graph.html").write_text(
            render_html(graph),
            encoding="utf-8",
        )
        crop_bindings = _copy_binding(
            root=temp_root,
            workflow=workflow,
            recording_dir=recording_dir,
            bundle_dir=bundle_dir,
        )
        manifest = _assemble_manifest(
            root=temp_root,
            pack_id=pack_id,
            provenance=provenance,
            environment=environment,
            environment_digest=environment_digest,
            app_digest=app_digest,
            workflow=workflow,
            qualification_report=qualification_report,
            cases=case_manifests,
            crop_bindings=crop_bindings,
            trials=trials,
        )
        _write_json(temp_root / "manifest.json", manifest)
        (temp_root / "manifest.sha256").write_text(
            f"{_sha256(temp_root / 'manifest.json')}  manifest.json\n",
            encoding="ascii",
        )
        validate_pack(temp_root)
        os.replace(temp_root, destination)
        return destination
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise
    finally:
        if stop is not None:
            stop()


def _safe_file(root: Path, relative: str) -> Path:
    if "\\" in relative:
        raise EvidencePackError(f"non-POSIX file path: {relative}")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise EvidencePackError(f"unsafe file path: {relative}")
    candidate = root.joinpath(*pure.parts)
    cursor = root
    for part in pure.parts:
        cursor /= part
        if cursor.is_symlink():
            raise EvidencePackError(f"symlink not permitted in pack: {relative}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise EvidencePackError(f"file leaves pack or is missing: {relative}") from exc
    if not resolved.is_file():
        raise EvidencePackError(f"pack reference is not a file: {relative}")
    return resolved


def validate_pack(pack_dir: Path | str) -> dict[str, Any]:
    root = Path(pack_dir).resolve(strict=True)
    manifest_path = _safe_file(root, "manifest.json")
    digest_path = _safe_file(root, "manifest.sha256")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(manifest)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise EvidencePackError("unsupported public-demo manifest schema")
    expected_digest = digest_path.read_text(encoding="ascii").split()[0]
    if _sha256(manifest_path) != expected_digest:
        raise EvidencePackError("manifest.sha256 does not bind manifest.json")
    if manifest.get("pack", {}).get("immutable") is not True:
        raise EvidencePackError("public-demo pack is not marked immutable")

    inventory: dict[str, dict[str, Any]] = {}
    for ref in manifest.get("files", []):
        path = str(ref.get("path", ""))
        if path in inventory:
            raise EvidencePackError(f"duplicate inventory path: {path}")
        actual = _safe_file(root, path)
        if actual.stat().st_size != ref.get("bytes") or _sha256(actual) != ref.get(
            "sha256"
        ):
            raise EvidencePackError(f"inventory mismatch: {path}")
        inventory[path] = ref
    actual_payloads = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not _is_pack_control(root, path)
    }
    for path in actual_payloads:
        _reject_run_authority(path)
    if actual_payloads != set(inventory):
        missing = sorted(actual_payloads.symmetric_difference(inventory))
        raise EvidencePackError(f"file inventory is not exhaustive: {missing}")

    def validate_refs(value: Any) -> None:
        if isinstance(value, dict):
            if {
                "path",
                "sha256",
                "bytes",
                "role",
                "media_type",
            }.issubset(value):
                path = str(value["path"])
                if inventory.get(path) != value:
                    raise EvidencePackError(
                        f"artifact ref does not match exhaustive inventory: {path}"
                    )
            for item in value.values():
                validate_refs(item)
        elif isinstance(value, list):
            for item in value:
                validate_refs(item)

    validate_refs(manifest["task"])
    validate_refs(manifest["artifacts"])

    artifacts = manifest["artifacts"]
    for binding in artifacts["crop_bindings"]:
        crop = _safe_file(root, binding["crop_path"])
        source = _safe_file(root, binding["source_frame_path"])
        if _sha256(crop) != binding["crop_sha256"]:
            raise EvidencePackError(f"crop hash mismatch: {binding['crop_path']}")
        if _sha256(source) != binding["source_frame_sha256"]:
            raise EvidencePackError(
                f"source frame hash mismatch: {binding['source_frame_path']}"
            )
        with Image.open(source) as raw_image, Image.open(crop) as crop_image:
            x, y, width, height = binding["region"]
            expected = raw_image.convert("RGB").crop((x, y, x + width, y + height))
            actual = crop_image.convert("RGB")
            if expected.size != actual.size or expected.tobytes() != actual.tobytes():
                raise EvidencePackError(
                    f"crop pixels are not bound to source frame: {binding['crop_path']}"
                )

    outcomes: Counter[str] = Counter()
    model_calls = 0
    silent_wrong = 0
    wrong_target = 0
    over_halts = 0
    for case in artifacts["cases"]:
        expected = case["expected_outcome"]
        if len(case["reports"]) < 3:
            raise EvidencePackError(f"{case['case_id']} has fewer than 3 trials")
        for report_ref, outcome_ref, oracle_ref in zip(
            case["reports"],
            case["outcome_envelopes"],
            case["oracles"],
            strict=True,
        ):
            report_path = _safe_file(root, report_ref["path"])
            report = RunReport.model_validate_json(
                report_path.read_text(encoding="utf-8")
            )
            outcome = json.loads(
                _safe_file(root, outcome_ref["path"]).read_text(encoding="utf-8")
            )
            oracle = json.loads(
                _safe_file(root, oracle_ref["path"]).read_text(encoding="utf-8")
            )
            if (
                outcome["report_sha256"] != _sha256(report_path)
                or outcome["observed_outcome"] != report.execution_outcome
                or outcome["expected_outcome"] != expected
                or not outcome["matched_expectation"]
                or not oracle["passed"]
            ):
                raise EvidencePackError(
                    f"case outcome/report/oracle mismatch: {case['case_id']}"
                )
            outcomes[report.execution_outcome or ""] += 1
            model_calls += report.model_calls
            silent_wrong += int(oracle["silent_incorrect_success"])
            wrong_target += int(oracle["wrong_target_action"])
            over_halts += int(
                report.execution_outcome == "HALTED" and expected != "HALTED"
            )
    evaluation = manifest["evaluation"]
    if (
        dict(sorted(outcomes.items())) != evaluation["outcome_counts"]
        or model_calls != evaluation["model_calls"]
        or silent_wrong != evaluation["silent_incorrect_successes"]
        or wrong_target != evaluation["wrong_target_actions"]
        or over_halts != evaluation["over_halts"]
        or model_calls != 0
        or silent_wrong != 0
        or wrong_target != 0
        or evaluation["external_network_calls"] != 0
        or evaluation["screenshots_may_leave_box"] is not False
        or evaluation["qualification_passed"] is not True
    ):
        raise EvidencePackError("evaluation aggregate does not match case evidence")
    if outcomes.get("VERIFIED", 0) < 3 or outcomes.get("HALTED", 0) < 15:
        raise EvidencePackError("pack lacks the required VERIFIED/HALTED evidence")
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "public-demo" / "evidence-packs",
        help="parent directory for the immutable pack",
    )
    parser.add_argument("--pack-id", default="mockmed-triage-v1")
    parser.add_argument("--trials", type=int, default=TRIALS_PER_CASE)
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="development only: allow evidence from an uncommitted source tree",
    )
    parser.add_argument(
        "--validate",
        type=Path,
        help="validate an existing pack instead of exporting",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.validate is not None:
        manifest = validate_pack(args.validate)
        print(
            f"VALID {manifest['pack']['id']}: "
            f"{len(manifest['files'])} files, "
            f"{manifest['evaluation']['run_count']} real runs"
        )
        return 0
    output = export_pack(
        output_root=args.out,
        pack_id=args.pack_id,
        trials=args.trials,
        allow_dirty=args.allow_dirty,
    )
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    print(
        f"Wrote immutable public-demo evidence pack {output} "
        f"({len(manifest['files'])} files; "
        f"{manifest['evaluation']['outcome_counts']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
