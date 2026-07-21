"""Create and verify operator runtime-validation attestations for hosted bundles.

Privacy approval answers whether an exact artifact may leave its source
boundary. It does not prove that a scrubbed workflow still behaves correctly.
This module keeps those decisions separate: it binds real local lint,
certification, and replay evidence to an expiring, tenant-scoped Cloud
challenge and to the exact approved recording and bundle archives.

The HMAC proves possession of the ingest token and prevents accidental or
in-transit mutation. It is an operator attestation, not independent remote
observation or a general safety certification.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote, urlsplit

import httpx
import idna

from openadapt_flow import __version__
from openadapt_flow.bundle_validation import (
    build_runtime_parameter_schema,
    compute_parameter_schema_digest,
)
from openadapt_flow.hosted import (
    _API_TIMEOUT,
    _auth_headers,
    _body_snippet,
    resolve_destination_policy,
    resolve_host,
    resolve_token,
)
from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.policy import evaluate_policy, lint_workflow, load_policy
from openadapt_flow.sanitized_artifact import (
    SanitizationError,
    load_and_verify_derivative,
    load_valid_approval,
)

SCHEMA = "openadapt.runtime-validation/v1"
RISK_CLASSES = ("low", "consequential")
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_NON_PUBLIC_SUFFIXES = (
    ".arpa",
    ".example",
    ".home",
    ".internal",
    ".invalid",
    ".lan",
    ".local",
    ".localdomain",
    ".localhost",
    ".onion",
    ".test",
)


class RuntimeValidationError(RuntimeError):
    """A runtime attestation could not be created or verified."""


def workflow_risk_class(workflow: Workflow) -> str:
    """Return the v1 hosted risk class derived from compiled step semantics."""
    return (
        "consequential"
        if any(step.risk == "irreversible" for step in workflow.steps)
        else "low"
    )


def _canonical_execution_host(raw_host: str, *, field: str) -> str:
    """Return a Cloud-compatible DNS/IPv4 host or conservatively refuse it.

    The Cloud contract uses WHATWG URL parsing. Python's URL parser differs for
    shorthand IPv4 forms and cannot serialize IPv6 into the current hostname
    allowlist shape, so those ambiguous inputs are refused rather than signed
    into an attestation Cloud will interpret differently.
    """
    host = raw_host.strip()
    if not host or host.endswith(".") or ":" in host:
        raise RuntimeValidationError(f"Invalid {field}: {raw_host!r}")
    try:
        host = idna.encode(host, uts46=True, std3_rules=True).decode("ascii").lower()
    except idna.IDNAError as exc:
        raise RuntimeValidationError(f"Invalid {field}: {raw_host!r}") from exc
    if len(host) > 253 or any(
        not _DNS_LABEL_RE.fullmatch(label) for label in host.split(".")
    ):
        raise RuntimeValidationError(f"Invalid {field}: {raw_host!r}")

    # Managed browser execution accepts public DNS names only. Literal IPs make
    # private/link-local SSRF too easy, while special-use names can resolve
    # differently inside the runner. The runner resolves and rechecks every
    # admitted name immediately before constructing its network allowlist.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise RuntimeValidationError(f"Invalid {field}: public DNS name required")
    if (
        "." not in host
        or host == "localhost"
        or any(host.endswith(suffix) for suffix in _NON_PUBLIC_SUFFIXES)
    ):
        raise RuntimeValidationError(f"Invalid {field}: public DNS name required")

    # WHATWG treats a host ending in a number as IPv4-like (including forms
    # such as 127.1 and integer/hex spellings). Sign only canonical dotted
    # decimal IPv4 so Python and Cloud cannot disagree about the destination.
    final_label = host.rsplit(".", 1)[-1]
    looks_numeric = final_label.isdigit() or (
        final_label.startswith("0x")
        and len(final_label) > 2
        and all(character in "0123456789abcdef" for character in final_label[2:])
    )
    if looks_numeric:
        try:
            address = ipaddress.IPv4Address(host)
        except ipaddress.AddressValueError as exc:
            raise RuntimeValidationError(f"Invalid {field}: {raw_host!r}") from exc
        if str(address) != host:
            raise RuntimeValidationError(f"Invalid {field}: {raw_host!r}")
    return host


def normalize_execution_scope(
    target_url: str, allowed_hosts: Optional[list[str]]
) -> dict[str, Any]:
    """Validate and canonicalize the signed browser entry URL and boundary."""
    if target_url != target_url.strip():
        raise RuntimeValidationError(
            "Target URL must not contain leading or trailing whitespace"
        )
    try:
        parsed = urlsplit(target_url)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeValidationError("Target URL contains an invalid port") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeValidationError(
            "Target URL must be HTTPS without credentials, query, or fragment"
        )
    target_host = _canonical_execution_host(
        parsed.hostname, field="target origin hostname"
    )
    authority = target_host if port in (None, 443) else f"{target_host}:{port}"
    normalized_origin = f"https://{authority}"
    path = parsed.path or "/"
    if "\\" in path or re.search(r"%(?![A-Fa-f0-9]{2})", path):
        raise RuntimeValidationError("Target URL contains an ambiguous path")
    if any(unquote(segment) in {".", ".."} for segment in path.split("/")):
        raise RuntimeValidationError("Target URL must not contain dot path segments")
    normalized_path = quote(
        path,
        safe="/%:@-._~!$&'()*+,;=",
    )
    entry_url = f"{normalized_origin}{normalized_path}"
    hosts = {target_host}
    for raw_host in allowed_hosts or []:
        host = _canonical_execution_host(raw_host, field="allowed host")
        hosts.add(host)
    if len(hosts) > 20:
        raise RuntimeValidationError("At most 20 allowed hosts may be attested")
    return {
        "entry_url": entry_url,
        "target_origin": normalized_origin,
        "allowed_hosts": sorted(hosts),
    }


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _signature(payload: dict[str, Any], token: str) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    return hmac.new(
        token.encode("utf-8"), _canonical_bytes(unsigned), hashlib.sha256
    ).hexdigest()


def _validate_challenge(challenge: Any) -> dict[str, str]:
    if not isinstance(challenge, dict):
        raise RuntimeValidationError("Validation challenge must be a JSON object")
    challenge_id = challenge.get("challenge_id")
    nonce = challenge.get("nonce")
    expires_at = challenge.get("expires_at")
    if not isinstance(challenge_id, str) or not 1 <= len(challenge_id) <= 200:
        raise RuntimeValidationError("Validation challenge id is invalid")
    if not isinstance(nonce, str) or not 16 <= len(nonce) <= 500:
        raise RuntimeValidationError("Validation challenge nonce is invalid")
    if not isinstance(expires_at, str) or not 1 <= len(expires_at) <= 100:
        raise RuntimeValidationError("Validation challenge expiry is invalid")
    try:
        normalized_expiry = (
            f"{expires_at[:-1]}+00:00" if expires_at.endswith("Z") else expires_at
        )
        expiry = datetime.fromisoformat(normalized_expiry)
    except ValueError as exc:
        raise RuntimeValidationError("Validation challenge expiry is invalid") from exc
    if expiry.tzinfo is None:
        raise RuntimeValidationError(
            "Validation challenge expiry must include a timezone"
        )
    if expiry <= datetime.now(timezone.utc):
        raise RuntimeValidationError("Validation challenge has expired")
    return {
        "challenge_id": challenge_id,
        "nonce": nonce,
        "expires_at": expires_at,
    }


def request_validation_challenge(
    *,
    host: Optional[str] = None,
    token: Optional[str] = None,
    destination_kind: Optional[str] = None,
    trusted_hosts: Optional[list[str]] = None,
) -> dict[str, str]:
    """Acquire a one-time, expiring validation challenge from Cloud."""
    resolved_host = resolve_host(host)
    resolve_destination_policy(
        resolved_host,
        destination_kind=destination_kind,
        trusted_hosts=trusted_hosts,
    )
    resolved_token = resolve_token(token, host=resolved_host)
    try:
        response = httpx.post(
            f"{resolved_host}/api/validation-challenges",
            headers=_auth_headers(resolved_token),
            timeout=_API_TIMEOUT,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise RuntimeValidationError(
            f"Could not acquire a validation challenge from {resolved_host}: {exc}"
        ) from exc
    if response.status_code == 401:
        raise RuntimeValidationError("Ingest token was rejected (401).")
    if response.status_code != 201:
        raise RuntimeValidationError(
            "Validation challenge returned "
            f"{response.status_code} (expected 201): {_body_snippet(response)}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeValidationError(
            "Cloud returned a non-JSON validation challenge"
        ) from exc
    challenge = body.get("challenge", body) if isinstance(body, dict) else {}
    return _validate_challenge(challenge)


def create_runtime_validation_attestation(
    *,
    recording_derivative: Path,
    bundle_derivative: Path,
    run_dir: Path,
    policy_source: str,
    risk_class: str,
    environment: str,
    target_url: str,
    allowed_hosts: Optional[list[str]] = None,
    compiler_config: Optional[dict[str, Any]] = None,
    host: Optional[str] = None,
    token: Optional[str] = None,
    challenge: Optional[dict[str, str]] = None,
    destination_kind: Optional[str] = None,
    trusted_hosts: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Build a signed attestation from actual local validation artifacts.

    ``recording_derivative`` and ``bundle_derivative`` must both be reviewed,
    approved sanitized artifacts. The bundle scrub must preserve its
    load-bearing bytes. Strict lint means zero findings; certification must
    pass the named policy; and ``run_dir/report.json`` must record a successful,
    non-halted replay of the same workflow.
    """
    recording_derivative = Path(recording_derivative)
    bundle_derivative = Path(bundle_derivative)
    run_dir = Path(run_dir)
    environment = environment.strip()
    if not environment or len(environment) > 200:
        raise RuntimeValidationError(
            "Environment identifier must be non-empty and at most 200 characters"
        )
    risk_class = risk_class.strip()
    if risk_class not in RISK_CLASSES:
        raise RuntimeValidationError(
            "Risk class must be one of: " + ", ".join(RISK_CLASSES)
        )
    execution = normalize_execution_scope(target_url, allowed_hosts)
    resolved_host = resolve_host(host)
    resolve_destination_policy(
        resolved_host,
        destination_kind=destination_kind,
        trusted_hosts=trusted_hosts,
    )
    resolved_token = resolve_token(token, host=resolved_host)
    if challenge is None:
        challenge = request_validation_challenge(
            host=resolved_host,
            token=resolved_token,
            destination_kind=destination_kind,
            trusted_hosts=trusted_hosts,
        )
    challenge = _validate_challenge(challenge)

    try:
        recording_manifest = load_and_verify_derivative(recording_derivative)
        recording_approval = load_valid_approval(recording_derivative)
        bundle_manifest = load_and_verify_derivative(bundle_derivative)
        bundle_approval = load_valid_approval(bundle_derivative)
    except SanitizationError as exc:
        raise RuntimeValidationError(str(exc)) from exc
    if recording_manifest.get("kind") != "recording":
        raise RuntimeValidationError("Validation source must be a recording derivative")
    if bundle_manifest.get("kind") != "bundle":
        raise RuntimeValidationError("Validation target must be a bundle derivative")
    if bundle_manifest.get("execution_semantics") != "preserved":
        raise RuntimeValidationError(
            "Bundle sanitization changed load-bearing bytes; validate a bundle "
            "compiled from the already-sanitized recording instead"
        )

    workflow = Workflow.load(bundle_derivative)
    derived_risk_class = workflow_risk_class(workflow)
    if risk_class != derived_risk_class:
        raise RuntimeValidationError(
            f"Risk class {risk_class!r} does not match compiled workflow risk "
            f"{derived_risk_class!r}"
        )
    provenance = workflow.manifest.provenance if workflow.manifest else None
    if provenance is None or not provenance.source_recording_sha256:
        raise RuntimeValidationError(
            "Bundle has no approved source-recording provenance; compile it "
            "from the approved sanitized recording"
        )
    if (
        provenance.source_recording_sha256
        != recording_approval["approved_derivative_sha256"]
    ):
        raise RuntimeValidationError(
            "Bundle provenance is bound to a different approved recording"
        )
    if not provenance.compiler_config_sha256:
        raise RuntimeValidationError("Bundle has no compiler-configuration provenance")
    if compiler_config is not None:
        supplied_config_sha256 = _sha256_json(compiler_config)
        if supplied_config_sha256 != provenance.compiler_config_sha256:
            raise RuntimeValidationError(
                "Compiler config does not match the bundle provenance"
            )
    lint_report = lint_workflow(workflow)
    if lint_report.findings:
        counts = lint_report.counts()
        raise RuntimeValidationError(
            "Strict lint failed: "
            f"{counts['error']} error, {counts['warn']} warn, {counts['info']} info"
        )

    try:
        policy = load_policy(policy_source)
    except (FileNotFoundError, ValueError) as exc:
        raise RuntimeValidationError(str(exc)) from exc
    certification = evaluate_policy(workflow, policy)
    if not certification.passed:
        raise RuntimeValidationError(
            f"Certification failed under policy {certification.policy_name!r}"
        )

    report_path = run_dir / "report.json"
    try:
        report_bytes = report_path.read_bytes()
        report = RunReport.model_validate_json(report_bytes)
    except (OSError, ValueError) as exc:
        raise RuntimeValidationError(f"Cannot read a valid run report: {exc}") from exc
    if not report.success or report.halt is not None:
        raise RuntimeValidationError(
            "Runtime validation requires a successful, non-halted replay"
        )
    if report.workflow_name != workflow.name:
        raise RuntimeValidationError(
            "Run report workflow does not match the approved bundle"
        )
    if (
        not workflow.manifest
        or report.bundle_content_digest != workflow.manifest.content_digest
    ):
        raise RuntimeValidationError("Run report is bound to a different bundle")
    if report.source_recording_sha256 != provenance.source_recording_sha256:
        raise RuntimeValidationError(
            "Run report source-recording provenance does not match"
        )
    parameter_schema_sha256 = compute_parameter_schema_digest(workflow)
    parameters = build_runtime_parameter_schema(workflow)
    if len(parameters) > 100:
        raise RuntimeValidationError(
            "At most 100 runtime parameters may be attested for hosted execution"
        )
    seen_parameter_names: set[str] = set()
    for parameter in parameters:
        name = parameter["name"]
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name)
            or name in seen_parameter_names
        ):
            raise RuntimeValidationError(
                f"Parameter name is not valid for hosted execution: {name!r}"
            )
        seen_parameter_names.add(name)
        if parameter["secret"] and parameter["choices"]:
            raise RuntimeValidationError(
                f"Secret parameter choices would expose runtime values: {name!r}"
            )
        if parameter["type"] == "enum" and not parameter["choices"]:
            raise RuntimeValidationError(
                f"Enum parameter has no allowed choices: {name!r}"
            )
        if len(parameter["choices"]) > 100 or any(
            not isinstance(choice, str) or len(choice) > 500
            for choice in parameter["choices"]
        ):
            raise RuntimeValidationError(
                f"Parameter choices are not valid for hosted execution: {name!r}"
            )
    if report.parameter_schema_sha256 != parameter_schema_sha256:
        raise RuntimeValidationError(
            "Run report parameter schema does not match the bundle"
        )
    if report.execution_origin != execution["target_origin"]:
        raise RuntimeValidationError(
            "Run report browser origin does not match the attested target origin"
        )
    if report.execution_entry_url != execution["entry_url"]:
        raise RuntimeValidationError(
            "Run report browser entry URL does not match the attested target URL"
        )

    compiler_version = provenance.compiler_version or __version__
    lint_evidence = lint_report.model_dump(mode="json")
    certification_evidence = certification.model_dump(mode="json")

    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "challenge_id": challenge["challenge_id"],
        "nonce": challenge["nonce"],
        "source_recording_sha256": recording_approval["approved_derivative_sha256"],
        "bundle_sha256": bundle_approval["approved_derivative_sha256"],
        "compiler": {
            "name": "openadapt-flow",
            "version": compiler_version or __version__,
            "config_sha256": provenance.compiler_config_sha256,
        },
        "parameter_schema_sha256": parameter_schema_sha256,
        "parameters": parameters,
        "execution": execution,
        "lint": {
            "strict": True,
            "passed": True,
            "evidence_sha256": _sha256_json(lint_evidence),
        },
        "certification": {
            "policy": certification.policy_name,
            "risk_class": risk_class,
            "passed": True,
            "evidence_sha256": _sha256_json(certification_evidence),
        },
        "replay": {
            "success": True,
            "report_sha256": _sha256_bytes(report_bytes),
            "environment_sha256": _sha256_json({"environment": environment}),
            "validated_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    payload["signature"] = _signature(payload, resolved_token)
    return payload


def verify_runtime_validation_attestation(
    attestation: dict[str, Any], *, bundle_sha256: str, token: str
) -> None:
    """Verify local signature and exact-bundle binding before upload."""
    if attestation.get("schema") != SCHEMA:
        raise RuntimeValidationError("Unsupported runtime-validation schema")
    if attestation.get("bundle_sha256") != bundle_sha256:
        raise RuntimeValidationError(
            "Runtime validation is bound to a different approved bundle"
        )
    signature = attestation.get("signature")
    if not isinstance(signature, str) or not hmac.compare_digest(
        signature, _signature(attestation, token)
    ):
        raise RuntimeValidationError("Runtime-validation signature is invalid")


def load_runtime_validation_attestation(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeValidationError(f"Cannot read runtime validation: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeValidationError("Runtime validation must be a JSON object")
    return value


def save_runtime_validation_attestation(
    attestation: dict[str, Any], path: Path
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(attestation, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path
