"""Zero-PHI pilot capture bundle (``.oapilot.zip``) and local sandbox runner.

Implements two buyer-safe deployment models without modifying the core CLI surface:

- **Model B (Record -> Local Redact -> ``.oapilot.zip``):** Packages a recorded
  trace, DOM/accessibility snapshots, optional screenshots, and metadata after
  deterministic local PII/secret scrubbing. Fails closed (``PilotRedactionError``)
  if any unredacted secret or PII pattern remains.
- **Model C (Local ``.oapack`` Verification Runner):** Executes a compiled pack
  locally inside the operator's own environment, runs independent read-back
  verification, and exports only a value-free Ed25519-signed ``record/run/v1``
  DSSE receipt verifiable via :func:`openadapt_types.receipt.verify_receipt`.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

if TYPE_CHECKING:
    from openadapt_types.receipt import RoleKey

PILOT_BUNDLE_SCHEMA = "openadapt.pilot-bundle/v1"
_MANIFEST_NAME = "manifest.json"
_METADATA_NAME = "metadata.json"
_TRACE_NAME = "trace.json"

_SENSITIVE_KEY_TOKENS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "private_key",
    "ssn",
    "social_security",
    "dob",
    "date_of_birth",
    "birth_date",
    "credit_card",
    "card_number",
    "cvv",
)

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "bearer_token",
        re.compile(r"\bBearer\s+[A-Za-z0-9._\-~+/]+=*\b", re.IGNORECASE),
    ),
    (
        "api_token",
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_\-]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b"
        ),
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    ),
    (
        "ssn",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    ),
    (
        "credit_card",
        re.compile(r"\b(?:\d{4}[-\s]){3}\d{4}\b"),
    ),
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    ),
    (
        "phone",
        re.compile(r"\b(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]\d{3}[-.\s]\d{4}\b"),
    ),
    (
        "dob_us",
        re.compile(r"\b(?:0[1-9]|1[0-2])/(?:0[1-9]|[12]\d|3[01])/(?:19|20)\d{2}\b"),
    ),
)


class PilotBundleError(Exception):
    """Base error for pilot bundle creation, inspection, and execution."""


class PilotRedactionError(PilotBundleError):
    """Raised fail-closed when unredacted PII or secrets remain before export."""


class PilotIntegrityError(PilotBundleError):
    """Raised fail-closed when a ``.oapilot.zip`` fails path or hash checks."""


@dataclass(frozen=True)
class RedactionReport:
    """Summary of local PII/secret scrubbing before bundle export."""

    fields_scrubbed: int
    patterns_matched: dict[str, int]
    secrets_remaining: int
    safe_to_share: bool
    used_openadapt_privacy: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "fields_scrubbed": self.fields_scrubbed,
            "patterns_matched": dict(sorted(self.patterns_matched.items())),
            "secrets_remaining": self.secrets_remaining,
            "safe_to_share": self.safe_to_share,
            "used_openadapt_privacy": self.used_openadapt_privacy,
        }


@dataclass(frozen=True)
class PilotBundle:
    """Metadata and manifest of a created ``.oapilot.zip`` bundle."""

    path: Path
    schema: str
    app_name: str
    workflow_name: str
    recorded_at: str
    step_count: int
    snapshot_count: int
    screenshot_count: int
    redaction_report: RedactionReport
    manifest_sha256: dict[str, str]


@dataclass(frozen=True)
class PilotBundleInspection:
    """Verified summary returned by :func:`inspect_pilot_bundle`."""

    path: Path
    schema: str
    app_name: str
    workflow_name: str
    recorded_at: str
    step_count: int
    snapshot_count: int
    screenshot_count: int
    redaction_report: RedactionReport
    verified_files: int

    def format_summary(self) -> str:
        """Concise human-readable summary of a verified pilot bundle."""
        status = "SAFE TO SHARE" if self.redaction_report.safe_to_share else "UNSAFE"
        return (
            f"Pilot Bundle: {self.path.name} ({self.schema})\n"
            f"  App / Workflow: {self.app_name} / {self.workflow_name}\n"
            f"  Recorded At:    {self.recorded_at}\n"
            f"  Contents:       {self.step_count} steps, "
            f"{self.snapshot_count} DOM snapshots, {self.screenshot_count} screenshots\n"
            f"  Integrity:      OK ({self.verified_files} files SHA-256 verified)\n"
            f"  Redaction:      {status} "
            f"(fields_scrubbed={self.redaction_report.fields_scrubbed}, "
            f"secrets_remaining={self.redaction_report.secrets_remaining})"
        )


@dataclass(frozen=True)
class PilotExecutionResult:
    """Result of local sandbox execution in Model C."""

    steps_total: int
    steps_ok: int
    writes_attempted: int
    succeeded: bool
    model_calls: int = 0
    reason_code: str | None = None


@dataclass(frozen=True)
class PilotVerificationResult:
    """Independent read-back verification result in Model C."""

    readback_confirmed: bool
    checks_required: int
    checks_confirmed: int
    reason_code: str | None = None


@dataclass(frozen=True)
class PilotRunOutcome:
    """Output of :func:`run_pilot_pack`: local outcome + value-free Ed25519 receipt."""

    outcome: Literal["VERIFIED", "STOPPED"]
    receipt_envelope: dict[str, Any]
    signer_key: RoleKey
    receipt_path: Path | None
    pack_sha256: str
    report_sha256: str
    execution: PilotExecutionResult
    verification: PilotVerificationResult


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json_bytes(obj: Any) -> bytes:
    return (
        json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("utf-8")


def _is_sensitive_key(key: str) -> bool:
    lowered = key.strip().lower()
    return any(token in lowered for token in _SENSITIVE_KEY_TOKENS)


def _try_openadapt_privacy_scrubber() -> Callable[[str], str] | None:
    """Return a text scrubber from ``openadapt_privacy`` if installed, else None."""
    try:
        import openadapt_privacy  # type: ignore[import-not-found]
    except Exception:
        return None
    scrub_fn = getattr(openadapt_privacy, "scrub_text", None)
    if callable(scrub_fn):
        return lambda text: str(scrub_fn(text))
    return None


def _scrub_string(
    text: str,
    patterns_matched: dict[str, int],
    optional_scrubber: Callable[[str], str] | None,
) -> str:
    value = optional_scrubber(text) if optional_scrubber is not None else text
    for name, regex in _SECRET_PATTERNS:
        value, count = regex.subn(f"[REDACTED:{name.upper()}]", value)
        if count:
            patterns_matched[name] = patterns_matched.get(name, 0) + count
    return value


def _scrub_node(
    node: Any,
    *,
    fields_scrubbed: list[int],
    patterns_matched: dict[str, int],
    optional_scrubber: Callable[[str], str] | None,
) -> Any:
    if isinstance(node, Mapping):
        cleaned: dict[str, Any] = {}
        for raw_key, raw_val in node.items():
            key = str(raw_key)
            if _is_sensitive_key(key) and raw_val not in (None, ""):
                fields_scrubbed[0] += 1
                cleaned[key] = f"[REDACTED:{key.upper()}]"
            else:
                cleaned[key] = _scrub_node(
                    raw_val,
                    fields_scrubbed=fields_scrubbed,
                    patterns_matched=patterns_matched,
                    optional_scrubber=optional_scrubber,
                )
        return cleaned
    if isinstance(node, list):
        return [
            _scrub_node(
                item,
                fields_scrubbed=fields_scrubbed,
                patterns_matched=patterns_matched,
                optional_scrubber=optional_scrubber,
            )
            for item in node
        ]
    if isinstance(node, tuple):
        return [
            _scrub_node(
                item,
                fields_scrubbed=fields_scrubbed,
                patterns_matched=patterns_matched,
                optional_scrubber=optional_scrubber,
            )
            for item in node
        ]
    if isinstance(node, str):
        return _scrub_string(node, patterns_matched, optional_scrubber)
    return node


def scan_unredacted_secrets(payload: Any) -> dict[str, int]:
    """Fail-closed scan over any JSON-serializable payload for unredacted PII/secrets."""
    findings: dict[str, int] = {}

    def _walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for raw_key, raw_val in node.items():
                key = str(raw_key)
                if (
                    _is_sensitive_key(key)
                    and isinstance(raw_val, str)
                    and raw_val.strip()
                    and not raw_val.startswith("[REDACTED:")
                ):
                    findings[f"field:{key.lower()}"] = (
                        findings.get(f"field:{key.lower()}", 0) + 1
                    )
                _walk(raw_val)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)
        elif isinstance(node, str):
            for name, regex in _SECRET_PATTERNS:
                matches = regex.findall(node)
                if matches:
                    findings[name] = findings.get(name, 0) + len(matches)

    _walk(payload)
    return findings


def scrub_payload(
    payload: Any, *, redact: bool = True
) -> tuple[Any, RedactionReport]:
    """Scrub a trace/snapshot payload and produce a fail-closed :class:`RedactionReport`."""
    optional_scrubber = _try_openadapt_privacy_scrubber() if redact else None
    fields_scrubbed = [0]
    patterns_matched: dict[str, int] = {}

    if redact:
        cleaned = _scrub_node(
            payload,
            fields_scrubbed=fields_scrubbed,
            patterns_matched=patterns_matched,
            optional_scrubber=optional_scrubber,
        )
    else:
        cleaned = payload

    remaining_map = scan_unredacted_secrets(cleaned)
    secrets_remaining = sum(remaining_map.values())
    report = RedactionReport(
        fields_scrubbed=fields_scrubbed[0],
        patterns_matched=patterns_matched,
        secrets_remaining=secrets_remaining,
        safe_to_share=(secrets_remaining == 0),
        used_openadapt_privacy=(optional_scrubber is not None),
    )
    return cleaned, report


def _validate_archive_member_name(name: str) -> None:
    """Reject path traversal, absolute paths, or Windows drive letters."""
    if not name or name.startswith(("/", "\\")):
        raise PilotIntegrityError(f"Unsafe absolute archive member path: {name!r}")
    if "\\" in name:
        raise PilotIntegrityError(f"Backslash disallowed in archive member path: {name!r}")
    parts = Path(name).parts
    if any(part in ("..", "") for part in parts):
        raise PilotIntegrityError(f"Path traversal detected in archive member: {name!r}")
    if ":" in parts[0]:
        raise PilotIntegrityError(f"Drive specifier disallowed in archive member: {name!r}")


def create_pilot_bundle(
    out_path: Path | str,
    *,
    app_name: str,
    workflow_name: str,
    actions: Sequence[Mapping[str, Any]],
    dom_snapshots: Mapping[str, Any] | None = None,
    screenshots: Mapping[str, bytes] | None = None,
    recorded_at: str | None = None,
    redact: bool = True,
) -> PilotBundle:
    """Create a SHA-256-manifested, locally redacted ``.oapilot.zip`` bundle.

    Raises :class:`PilotRedactionError` fail-closed if any unredacted secret or
    PII pattern remains after scrubbing (for example if ``redact=False`` is
    passed on dirty input).
    """
    target = Path(out_path)
    timestamp = recorded_at or _utc_now_iso()

    raw_bundle_payload: dict[str, Any] = {
        "trace": {
            "schema": PILOT_BUNDLE_SCHEMA,
            "app_name": app_name,
            "workflow_name": workflow_name,
            "recorded_at": timestamp,
            "actions": [dict(a) for a in actions],
        },
        "dom_snapshots": dict(dom_snapshots or {}),
    }

    cleaned_payload, report = scrub_payload(raw_bundle_payload, redact=redact)
    if not report.safe_to_share or report.secrets_remaining > 0:
        raise PilotRedactionError(
            f"Refusing to export pilot bundle: {report.secrets_remaining} "
            "unredacted PII/secret finding(s) remain."
        )

    files_to_write: dict[str, bytes] = {}
    files_to_write[_TRACE_NAME] = _canonical_json_bytes(cleaned_payload["trace"])

    cleaned_snaps: Mapping[str, Any] = cleaned_payload["dom_snapshots"]
    for snap_name in sorted(cleaned_snaps):
        rel = f"dom_snapshots/{snap_name}.json"
        _validate_archive_member_name(rel)
        files_to_write[rel] = _canonical_json_bytes(cleaned_snaps[snap_name])

    screenshot_map = screenshots or {}
    for shot_name in sorted(screenshot_map):
        rel = f"screenshots/{shot_name}"
        _validate_archive_member_name(rel)
        files_to_write[rel] = bytes(screenshot_map[shot_name])

    metadata = {
        "schema": PILOT_BUNDLE_SCHEMA,
        "app_name": app_name,
        "workflow_name": workflow_name,
        "recorded_at": timestamp,
        "step_count": len(actions),
        "snapshot_count": len(cleaned_snaps),
        "screenshot_count": len(screenshot_map),
        "redaction_report": report.to_dict(),
    }
    files_to_write[_METADATA_NAME] = _canonical_json_bytes(metadata)

    manifest_hashes = {
        rel: _sha256_bytes(files_to_write[rel]) for rel in sorted(files_to_write)
    }
    manifest_doc = {
        "schema": PILOT_BUNDLE_SCHEMA,
        "files": manifest_hashes,
    }
    files_to_write[_MANIFEST_NAME] = _canonical_json_bytes(manifest_doc)

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=".oapilot-", suffix=".tmp"
    )
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_name, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for rel in sorted(files_to_write):
                zf.writestr(rel, files_to_write[rel])
        os.replace(tmp_name, target)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise

    return PilotBundle(
        path=target,
        schema=PILOT_BUNDLE_SCHEMA,
        app_name=app_name,
        workflow_name=workflow_name,
        recorded_at=timestamp,
        step_count=len(actions),
        snapshot_count=len(cleaned_snaps),
        screenshot_count=len(screenshot_map),
        redaction_report=report,
        manifest_sha256=manifest_hashes,
    )


def inspect_pilot_bundle(zip_path: Path | str) -> PilotBundleInspection:
    """Verify ZIP safety, SHA-256 manifest, and redaction metadata fail-closed."""
    path = Path(zip_path)
    if not path.is_file():
        raise PilotIntegrityError(f"Pilot bundle not found: {path}")

    try:
        zf = zipfile.ZipFile(path, mode="r")
    except zipfile.BadZipFile as exc:
        raise PilotIntegrityError(f"Corrupt pilot bundle ZIP: {path}") from exc

    with zf:
        members = zf.namelist()
        seen: set[str] = set()
        for member in members:
            _validate_archive_member_name(member)
            if member in seen:
                raise PilotIntegrityError(f"Duplicate archive member: {member!r}")
            seen.add(member)

        if _MANIFEST_NAME not in seen:
            raise PilotIntegrityError("Pilot bundle is missing manifest.json")
        if _METADATA_NAME not in seen:
            raise PilotIntegrityError("Pilot bundle is missing metadata.json")

        try:
            manifest = json.loads(zf.read(_MANIFEST_NAME).decode("utf-8"))
        except Exception as exc:
            raise PilotIntegrityError("manifest.json is not valid JSON") from exc

        if not isinstance(manifest, dict) or manifest.get("schema") != PILOT_BUNDLE_SCHEMA:
            raise PilotIntegrityError("Unsupported or malformed pilot bundle schema")

        manifest_files = manifest.get("files")
        if not isinstance(manifest_files, dict) or not manifest_files:
            raise PilotIntegrityError("manifest.json must list non-empty file hashes")

        expected_members = set(manifest_files.keys()) | {_MANIFEST_NAME}
        if seen != expected_members:
            missing = sorted(expected_members - seen)
            extra = sorted(seen - expected_members)
            raise PilotIntegrityError(
                f"Archive members do not match manifest (missing={missing}, extra={extra})"
            )

        for rel, expected_hash in sorted(manifest_files.items()):
            actual_hash = _sha256_bytes(zf.read(rel))
            if actual_hash != expected_hash:
                raise PilotIntegrityError(
                    f"SHA-256 mismatch for {rel!r}: expected {expected_hash}, got {actual_hash}"
                )

        try:
            metadata = json.loads(zf.read(_METADATA_NAME).decode("utf-8"))
        except Exception as exc:
            raise PilotIntegrityError("metadata.json is not valid JSON") from exc

        raw_report = metadata.get("redaction_report")
        if not isinstance(raw_report, dict):
            raise PilotIntegrityError("metadata.json is missing redaction_report")

        report = RedactionReport(
            fields_scrubbed=int(raw_report.get("fields_scrubbed", 0)),
            patterns_matched={
                str(k): int(v)
                for k, v in dict(raw_report.get("patterns_matched", {})).items()
            },
            secrets_remaining=int(raw_report.get("secrets_remaining", 0)),
            safe_to_share=bool(raw_report.get("safe_to_share", False)),
            used_openadapt_privacy=bool(raw_report.get("used_openadapt_privacy", False)),
        )
        if not report.safe_to_share or report.secrets_remaining != 0:
            raise PilotIntegrityError(
                "Pilot bundle metadata reports unredacted secrets remaining"
            )

        return PilotBundleInspection(
            path=path,
            schema=str(metadata.get("schema", "")),
            app_name=str(metadata.get("app_name", "")),
            workflow_name=str(metadata.get("workflow_name", "")),
            recorded_at=str(metadata.get("recorded_at", "")),
            step_count=int(metadata.get("step_count", 0)),
            snapshot_count=int(metadata.get("snapshot_count", 0)),
            screenshot_count=int(metadata.get("screenshot_count", 0)),
            redaction_report=report,
            verified_files=len(manifest_files),
        )


def run_pilot_pack(
    pack_path: Path | str,
    *,
    executor: Callable[[dict[str, Any]], PilotExecutionResult],
    verifier: Callable[[dict[str, Any], PilotExecutionResult], PilotVerificationResult],
    receipt_out: Path | str | None = None,
    signer: Ed25519PrivateKey | None = None,
    started_hour: str | None = None,
) -> PilotRunOutcome:
    """Execute a compiled pack locally and emit a value-free Ed25519 ``record/run/v1`` receipt.

    Zero credentials or field values are embedded in the signed receipt; the
    receipt is verifiable via :func:`openadapt_types.receipt.verify_receipt`.
    """
    from openadapt_types.receipt import (
        CheckCount,
        Checks,
        Digest,
        Disclosure,
        Line,
        Program,
        RoleKey,
        RunPredicateV1,
        RunStatementV1,
        Steps,
        Subject,
        key_id,
        sign_statement,
    )

    pack_file = Path(pack_path)
    if not pack_file.is_file():
        raise PilotBundleError(f"Compiled pack not found: {pack_file}")
    pack_bytes = pack_file.read_bytes()
    pack_sha256 = _sha256_bytes(pack_bytes)

    try:
        pack_data = json.loads(pack_bytes.decode("utf-8"))
    except Exception as exc:
        raise PilotBundleError(f"Compiled pack is not valid JSON: {pack_file}") from exc
    if not isinstance(pack_data, dict):
        raise PilotBundleError("Compiled pack root must be a JSON object")

    workflow_name = str(pack_data.get("workflow_name") or pack_file.stem)
    prog_id = "prog-" + _sha256_bytes(workflow_name.encode("utf-8"))[:16]

    exec_result = executor(pack_data)
    verify_result = verifier(pack_data, exec_result)

    verified = (
        exec_result.succeeded
        and verify_result.readback_confirmed
        and verify_result.checks_required > 0
        and verify_result.checks_confirmed == verify_result.checks_required
    )
    outcome: Literal["VERIFIED", "STOPPED"] = "VERIFIED" if verified else "STOPPED"

    writes_count = max(1, exec_result.writes_attempted)
    lines: list[Line] = []
    if verified:
        for idx in range(writes_count):
            lines.append(
                Line(
                    step=idx + 1,
                    target_kind="gui",
                    operation="write",
                    check="confirmed",
                    strength="read_back",
                    not_checked=[],
                )
            )
        lines_confirmed = writes_count
        lines_refuted = 0
        reason_code: str | None = None
    else:
        for idx in range(writes_count):
            lines.append(
                Line(
                    step=idx + 1,
                    target_kind="gui",
                    operation="write",
                    check="refuted",
                    strength="read_back",
                    not_checked=[],
                )
            )
        lines_confirmed = 0
        lines_refuted = writes_count
        reason_code = (
            verify_result.reason_code
            or exec_result.reason_code
            or "readback_mismatch"
        )

    hour_str = started_hour or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")

    # Value-free local report digest (no raw inputs or field values).
    local_report_summary = {
        "program_id": prog_id,
        "pack_sha256": pack_sha256,
        "outcome": outcome,
        "steps_total": exec_result.steps_total,
        "steps_ok": exec_result.steps_ok,
        "checks_required": verify_result.checks_required,
        "checks_confirmed": verify_result.checks_confirmed,
    }
    report_sha256 = _sha256_bytes(_canonical_json_bytes(local_report_summary))

    statement = RunStatementV1(
        _type="https://in-toto.io/Statement/v1",
        subject=[
            Subject(name="report.json", digest=Digest(sha256=report_sha256)),
            Subject(name="bundle", digest=Digest(sha256=pack_sha256)),
        ],
        predicateType="https://openadapt.ai/record/run/v1",
        predicate=RunPredicateV1(
            program=Program(id=prog_id, version_sha256=pack_sha256),
            started_hour=hour_str,
            outcome=outcome,
            reason_code=reason_code,
            steps=Steps(total=exec_result.steps_total, ok=exec_result.steps_ok),
            lines=lines,
            checks=Checks(
                identity=CheckCount(required=0, confirmed=0),
                postconditions=CheckCount(required=0, confirmed=0),
                effects=CheckCount(
                    required=verify_result.checks_required,
                    confirmed=verify_result.checks_confirmed,
                ),
                authorization=CheckCount(required=0, confirmed=0),
                lines_confirmed=lines_confirmed,
                lines_refuted=lines_refuted,
                lines_not_checked=0,
            ),
            model_calls=exec_result.model_calls,
            disclosure=Disclosure(
                version="openadapt.public-disclosure/v1",
                commitments={},
            ),
        ),
    )

    key = signer or Ed25519PrivateKey.generate()
    pub_raw = key.public_key().public_bytes_raw()
    kid = key_id(pub_raw)
    envelope = sign_statement(statement.wire(), [(kid, key.sign)])
    signer_role_key = RoleKey(
        key_id=kid,
        public_key=base64.b64encode(pub_raw).decode("ascii"),
        role="this_computer",
    )

    written_path: Path | None = None
    if receipt_out is not None:
        written_path = Path(receipt_out)
        written_path.parent.mkdir(parents=True, exist_ok=True)
        written_path.write_text(
            json.dumps(envelope, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return PilotRunOutcome(
        outcome=outcome,
        receipt_envelope=envelope,
        signer_key=signer_role_key,
        receipt_path=written_path,
        pack_sha256=pack_sha256,
        report_sha256=report_sha256,
        execution=exec_result,
        verification=verify_result,
    )


def main(argv: list[str] | None = None) -> int:
    """Standalone CLI entrypoint: ``python -m openadapt_flow.pilot_bundle``."""
    parser = argparse.ArgumentParser(
        prog="openadapt-pilot-bundle",
        description="Zero-PHI pilot bundle capture, inspection, and local verification.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_cap = sub.add_parser("capture", help="Create a locally redacted .oapilot.zip bundle.")
    p_cap.add_argument("--trace", type=Path, required=True, help="JSON trace file.")
    p_cap.add_argument("--out", type=Path, required=True, help="Output .oapilot.zip path.")
    p_cap.add_argument("--app", required=True, help="Application name.")
    p_cap.add_argument("--workflow", required=True, help="Workflow name.")

    p_ins = sub.add_parser("inspect", help="Verify and summarize a .oapilot.zip bundle.")
    p_ins.add_argument("bundle", type=Path, help="Path to .oapilot.zip bundle.")

    args = parser.parse_args(argv)
    if args.command == "capture":
        raw = json.loads(args.trace.read_text(encoding="utf-8"))
        actions = raw.get("actions", []) if isinstance(raw, dict) else raw
        snaps = raw.get("dom_snapshots", {}) if isinstance(raw, dict) else {}
        bundle = create_pilot_bundle(
            args.out,
            app_name=args.app,
            workflow_name=args.workflow,
            actions=actions,
            dom_snapshots=snaps,
        )
        print(
            json.dumps(
                {
                    "bundle": str(bundle.path),
                    "steps": bundle.step_count,
                    "redaction": bundle.redaction_report.to_dict(),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    inspection = inspect_pilot_bundle(args.bundle)
    print(inspection.format_summary())
    return 0


__all__ = [
    "PILOT_BUNDLE_SCHEMA",
    "PilotBundle",
    "PilotBundleError",
    "PilotBundleInspection",
    "PilotExecutionResult",
    "PilotIntegrityError",
    "PilotRedactionError",
    "PilotRunOutcome",
    "PilotVerificationResult",
    "RedactionReport",
    "create_pilot_bundle",
    "inspect_pilot_bundle",
    "run_pilot_pack",
    "scan_unredacted_secrets",
    "scrub_payload",
]


if __name__ == "__main__":
    raise SystemExit(main())
