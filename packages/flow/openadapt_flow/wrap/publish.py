"""Publish a wrapped task's admitted program as an OpenAdapt Cloud endpoint.

``openadapt wrap publish --task TASK.json`` ships the program that
``openadapt wrap`` already compiled, checked and admitted for that task, with
one credential, ``OPENADAPT_API_KEY``. The upload is the governed bundle
``push`` (local sanitization, a person's approval, a runtime-validation
attestation, exact-hash archive) sent to the organization's API-key workflow
route, and the endpoint is the cloud's workflow Execute API. What this module
adds is the part between them:

* it finds the task's admitted program from the task file alone,
* it refuses a program the cloud could not read back: no saving step, a
  saving step with no effect contract, or no confirmed ``verify.yaml``,
* it re-verifies the admission signature before anything leaves the machine,
* it stages the bundle with its read-back configuration and a small manifest,
* it sanitizes the bundle and the recording it was compiled from, shows one
  short summary and asks once (``--yes`` answers for a script),
* it finds the key's organization (``GET /api/v1/whoami``, or ``--org``),
* it registers this machine's admission public key with that organization
  once, so the cloud can check the program's admission signature,
* it attests the program from the wrap run's own evidence (its recording and
  its attended canary run) against a challenge bound to the same key, so no
  separate validation command is needed,
* it uploads to ``POST /api/v1/org/{org}/workflow`` (or ``PUT
  .../workflow/{id}/definition`` for a new version), then reads the
  endpoint's current definition with the same key and prints the URL and a
  ``curl`` example from the acknowledgment's ``execute_path`` and
  ``qualification_id``.

No credential is printed, logged or written to the publish record.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

#: The caller's OpenAdapt Execute API key. The older name used by
#: ``cloud_seal`` is read when the new one is unset.
API_KEY_ENVS = ("OPENADAPT_API_KEY", "OPENADAPT_EXECUTE_API_KEY")
#: Overrides the organization the API key reports through ``WHOAMI_PATH``.
ORG_ENV = "OPENADAPT_ORG_ID"
WHOAMI_PATH = "/api/v1/whoami"
PUBLISH_SCHEMA = "openadapt.wrap-publish/v1"
MANIFEST_NAME = "openadapt-wrap-publish.json"
READBACK_PATH = "readback/verify.yaml"
EXECUTE_REQUEST_SCHEMA = "openadapt.execute-request/v1"

#: Exit codes. 0: published and callable (or published with no key to check).
EXIT_REFUSED = 1
EXIT_SETUP = 2
EXIT_WAITING = 3

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
#: Server values embedded in a printed shell command must be inert there.
_SHELL_SAFE = re.compile(r"[A-Za-z0-9._:/+=-]{1,256}\Z")
_CODE = re.compile(r"[a-z0-9_]{1,64}\Z")
_PUBLIC_KEY_HEX = re.compile(r"[0-9a-f]{64}\Z")
_API_TIMEOUT = 15.0


class PublishError(Exception):
    """A fixed refusal: ``code`` is stable, ``message`` names what is missing.

    Messages never carry task values, record contents or credentials.
    """

    def __init__(self, code: str, message: str, *, exit_code: int = EXIT_REFUSED):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


@dataclass(frozen=True)
class StagedProgram:
    """The admitted program, staged for upload with its read-back check."""

    staged: Path
    manifest: dict[str, Any]
    record_dir: Path
    #: The verified admission manifest (its digest pins the evidence).
    admission: Mapping[str, Any]
    #: The task's cache directory (``program/`` and ``runs/`` live there).
    task_dir: Path


# -- local: find, check and stage -----------------------------------------------


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _readback_config(checks: Path) -> None:
    """Require a confirmed ``verify.yaml`` with a system-of-record reader."""
    import yaml

    if checks.is_symlink() or not checks.is_file():
        raise PublishError(
            "NO_READ_BACK",
            "the task has no confirmed read-back checks (verify.yaml). "
            "Confirm the proposed checks when the first run offers them, "
            "then admit the program",
        )
    try:
        document = yaml.safe_load(checks.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise PublishError(
            "NO_READ_BACK", "the confirmed read-back checks cannot be parsed"
        ) from None
    if (
        not isinstance(document, dict)
        or not isinstance(document.get("verifier"), dict)
        or not document["verifier"]
    ):
        raise PublishError(
            "NO_READ_BACK",
            "the confirmed checks name no verifier that reads the system of record",
        )


def _record_check(bundle: Path) -> tuple[dict[str, Any], str, str]:
    """The program's saves, the record-check digest and the content digest."""
    from openadapt_flow.ir import Workflow
    from openadapt_flow.jit.signature import record_check_digest, record_check_of

    try:
        workflow = Workflow.load(bundle)
    except Exception:  # noqa: BLE001 - a bundle that will not load is not shipped
        raise PublishError(
            "BUNDLE_UNREADABLE", "the admitted bundle cannot be loaded"
        ) from None
    check = record_check_of(workflow, literals=False)
    saves = check["saves"]
    if not saves:
        raise PublishError(
            "NO_READ_BACK",
            "the program has no saving step, so there is no write to read back. "
            "Publishing is for tasks that write to a system of record",
        )
    if any(not save["effects"] for save in saves):
        raise PublishError(
            "NO_READ_BACK",
            "a saving step in the program has no effect contract to read back",
        )
    digest = workflow.manifest.content_digest if workflow.manifest else None
    if not digest:
        raise PublishError(
            "BUNDLE_UNREADABLE", "the admitted bundle carries no content digest"
        )
    return check, record_check_digest(workflow), str(digest)


def stage(
    cache: Any,
    instruction: str,
    parameters: Sequence[str],
    *,
    key_dir: Optional[Path] = None,
) -> StagedProgram:
    """Check the task's admitted program and stage it for the governed push.

    Refuses, with nothing written, a held task, a task without an admitted
    program, an admission whose signature does not verify, and a program
    without a read-back check.
    """
    from openadapt_flow.jit.lifecycle import AdmissionError, verify_admission

    names = dict.fromkeys(parameters, "")
    held = getattr(cache, "held", None)
    if callable(held) and held(instruction, names):
        raise PublishError(
            "TASK_HELD",
            "this task is held after a failed or interrupted run. Reconcile or "
            "repair it before publishing",
        )
    program = cache.admitted_program(instruction, names)
    if program is None:
        raise PublishError(
            "NOT_ADMITTED",
            "this task has no admitted program yet. Run it once with "
            "`openadapt wrap --task TASK --request-id ID --param NAME=VALUE "
            "--max-cost-usd N`, confirm its checks and admit it, then publish",
        )
    bundle = Path(program["bundle"])
    checks = Path(program["checks"])
    try:
        admission = verify_admission(bundle, key_dir=key_dir)
    except AdmissionError:
        raise PublishError(
            "ADMISSION_INVALID",
            "the program's admission does not verify with the local admission "
            "key. Pass the --key-dir that admitted it, or admit it again",
        ) from None
    _readback_config(checks)
    check, check_digest, content_digest = _record_check(bundle)
    if any(path.is_symlink() for path in bundle.rglob("*")):
        raise PublishError(
            "BUNDLE_UNSAFE", "the admitted bundle contains a symbolic link"
        )

    record_dir = (
        Path(program["task_dir"]).parent / "publish" / Path(program["task_dir"]).name
    )
    staged = record_dir / "bundle"
    if record_dir.is_symlink() or staged.is_symlink():
        raise PublishError("BUNDLE_UNSAFE", "the publish directory is a symbolic link")
    record_dir.mkdir(parents=True, exist_ok=True)
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(bundle, staged, symlinks=True)
    (staged / READBACK_PATH).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(checks, staged / READBACK_PATH)
    manifest = {
        "schema": PUBLISH_SCHEMA,
        "parameters": list(parameters),
        "input_schema": {
            "type": "object",
            "properties": {name: {"type": "string"} for name in parameters},
            "required": list(parameters),
            "additionalProperties": False,
        },
        "workflow_digest": content_digest,
        "record_check_sha256": check_digest,
        "saves": len(check["saves"]),
        "readback": {
            "path": READBACK_PATH,
            "sha256": _sha256_file(staged / READBACK_PATH),
        },
    }
    (staged / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return StagedProgram(
        staged=staged,
        manifest=manifest,
        record_dir=record_dir,
        admission=admission if isinstance(admission, Mapping) else {},
        task_dir=Path(program["task_dir"]),
    )


# -- cloud: endpoint and definition ---------------------------------------------


def api_key(environ: Mapping[str, str] | None = None) -> Optional[str]:
    """The caller's API key from the environment, never from a file or flag."""
    env = os.environ if environ is None else environ
    for name in API_KEY_ENVS:
        value = env.get(name, "").strip()
        if value:
            return value
    return None


def endpoint_url(host: str, org_id: str, workflow_id: str) -> str:
    if not (_UUID.fullmatch(org_id) and _UUID.fullmatch(workflow_id)):
        raise PublishError(
            "CLOUD_RESPONSE_INVALID", "the cloud returned an invalid workflow identity"
        )
    return f"{host.rstrip('/')}/api/v1/org/{org_id}/workflow/{workflow_id}"


def org_route(host: str, org_id: str, suffix: str) -> str:
    if not _UUID.fullmatch(org_id):
        raise PublishError("ORG_INVALID", "the organization id is not a UUID")
    return f"{host.rstrip('/')}/api/v1/org/{org_id}/{suffix}"


def upload_route(org_id: str, workflow_id: Optional[str]) -> tuple[str, str]:
    """The API-key workflow route: create, or a new version of ``workflow_id``."""
    if not _UUID.fullmatch(org_id):
        raise PublishError("ORG_INVALID", "the organization id is not a UUID")
    if workflow_id is None:
        return "POST", f"/api/v1/org/{org_id}/workflow"
    normalized = str(workflow_id).strip().lower()
    if not _UUID.fullmatch(normalized):
        raise PublishError(
            "WORKFLOW_ID_INVALID",
            "--workflow-id must be the workflow's id (a UUID)",
            exit_code=EXIT_SETUP,
        )
    return "PUT", f"/api/v1/org/{org_id}/workflow/{normalized}/definition"


def _call(
    method: str,
    url: str,
    key: str,
    *,
    body: Optional[Mapping[str, Any]] = None,
    http: Any = None,
) -> tuple[Optional[int], Any]:
    """One JSON request with the key as a Bearer header; never raises on HTTP."""
    import httpx

    client = http if http is not None else httpx
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    try:
        if method == "GET":
            response = client.get(
                url, headers=headers, timeout=_API_TIMEOUT, follow_redirects=False
            )
        else:
            response = client.post(
                url,
                headers=headers,
                json=dict(body or {}),
                timeout=_API_TIMEOUT,
                follow_redirects=False,
            )
    except httpx.HTTPError:
        return None, None
    try:
        parsed = response.json()
    except ValueError:
        parsed = None
    return response.status_code, parsed


def _workflow_api_unavailable(host: str) -> PublishError:
    return PublishError(
        "WORKFLOW_API_UNAVAILABLE",
        f"the workflow API is not enabled for this organization on {host}",
        exit_code=EXIT_SETUP,
    )


def resolve_org(
    host: str,
    key: str,
    override: Optional[str] = None,
    *,
    environ: Mapping[str, str] | None = None,
    http: Any = None,
) -> str:
    """The organization the API key belongs to.

    ``--org`` or ``$OPENADAPT_ORG_ID`` wins; otherwise the cloud says, with
    the same key (``GET /api/v1/whoami`` -> ``{"org_id": ...}``).
    """
    env = os.environ if environ is None else environ
    value = (override or env.get(ORG_ENV, "")).strip().lower()
    if value:
        if not _UUID.fullmatch(value):
            raise PublishError(
                "ORG_INVALID",
                "--org / OPENADAPT_ORG_ID must be the organization's id (a UUID)",
                exit_code=EXIT_SETUP,
            )
        return value
    status, body = _call("GET", f"{host.rstrip('/')}{WHOAMI_PATH}", key, http=http)
    if status == 401:
        raise _key_rejected()
    org = body.get("org_id") if status == 200 and isinstance(body, dict) else None
    if isinstance(org, str) and _UUID.fullmatch(org):
        return org
    raise PublishError(
        "ORG_UNKNOWN",
        "the cloud did not say which organization OPENADAPT_API_KEY belongs to. "
        "Pass --org ORG_ID or set OPENADAPT_ORG_ID",
        exit_code=EXIT_SETUP,
    )


def admission_public_key(key_dir: Optional[Path]) -> tuple[str, str]:
    """This machine's admission public key (hex) and its fingerprint."""
    from openadapt_flow.jit.lifecycle import default_key_dir

    keys = Path(key_dir) if key_dir is not None else default_key_dir()
    pub = keys / "keys" / "ed25519.pub"
    text = ""
    if pub.is_file() and not pub.is_symlink():
        try:
            text = pub.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            text = ""
    if not _PUBLIC_KEY_HEX.fullmatch(text):
        raise PublishError(
            "ADMISSION_INVALID",
            "the local admission public key (keys/ed25519.pub) is missing or not "
            "64 hex characters. Pass the --key-dir that admitted the program",
        )
    return text, "sha256:" + hashlib.sha256(bytes.fromhex(text)).hexdigest()


def register_admission_key(
    host: str,
    key: str,
    org_id: str,
    public_key: str,
    fingerprint: str,
    *,
    http: Any = None,
) -> bool:
    """Make the organization trust this admission key. Idempotent.

    Lists the organization's keys first and registers only when this one is
    missing. Returns True when it was registered by this call.
    """
    url = org_route(host, org_id, "admission-keys")
    status, body = _call("GET", url, key, http=http)
    if status == 401:
        raise _key_rejected()
    if status == 404:
        raise _workflow_api_unavailable(host)
    rows = body.get("admission_keys") if isinstance(body, dict) else None
    if status != 200 or not isinstance(rows, list):
        raise PublishError(
            "ADMISSION_KEY_UNREGISTERED",
            "the organization's admission keys could not be read. Nothing was uploaded",
        )
    if any(
        isinstance(row, dict) and row.get("fingerprint") == fingerprint for row in rows
    ):
        return False
    status, body = _call("POST", url, key, body={"public_key": public_key}, http=http)
    if status == 401:
        raise _key_rejected()
    if status == 409:
        raise PublishError(
            "ADMISSION_KEY_REVOKED",
            "this machine's admission key was revoked for the organization. "
            "Admit the program again with a new --key-dir, then publish",
        )
    if (
        status in (200, 201)
        and isinstance(body, dict)
        and body.get("fingerprint") == fingerprint
    ):
        return status == 201
    raise PublishError(
        "ADMISSION_KEY_UNREGISTERED",
        "the cloud did not register this machine's admission key. Nothing was uploaded",
    )


def fetch_definition(
    host: str,
    key: str,
    *,
    org_id: str,
    workflow_id: str,
    qualification_id: str,
    http: Any = None,
) -> dict[str, Any]:
    """Read the endpoint's current callable definition. Never dispatches a run.

    Returns ``{"callable": True, "definition": ...}`` or
    ``{"callable": False, "reason": <fixed text>, "code": <server code|None>}``.
    """
    import httpx

    client = http if http is not None else httpx
    url = endpoint_url(host, org_id, workflow_id) + "/definition"
    try:
        response = client.get(
            url,
            params={"qualification_id": qualification_id},
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=_API_TIMEOUT,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        return {
            "callable": False,
            "reason": "the cloud could not be reached",
            "code": None,
        }
    try:
        body = response.json()
    except ValueError:
        body = None
    code = body.get("code") if isinstance(body, dict) else None
    code = code if isinstance(code, str) and _CODE.fullmatch(code) else None
    status = response.status_code
    if status == 401:
        return {"callable": False, "reason": "the API key was rejected", "code": None}
    if status == 404:
        return {
            "callable": False,
            "reason": (
                "the endpoint is not visible to this API key. Check that the key "
                "belongs to this organization and allows this qualification, and "
                "that the workflow API is enabled for the organization"
            ),
            "code": None,
        }
    if status == 409:
        return {
            "callable": False,
            "reason": (
                "the workflow is uploaded but not callable yet. Approve its first "
                "run in the dashboard if your organization requires that"
            ),
            "code": code,
        }
    if status != 200 or not isinstance(body, dict):
        return {
            "callable": False,
            "reason": "the cloud did not return a definition",
            "code": code,
        }
    binding = body.get("binding")
    expected_path = f"/api/v1/org/{org_id}/workflow/{workflow_id}"
    if (
        body.get("org_id") != org_id
        or body.get("workflow_id") != workflow_id
        or body.get("method") != "POST"
        or body.get("path") != expected_path
        or not isinstance(binding, dict)
        or binding.get("qualification_id") != qualification_id
        or not isinstance(body.get("parameter_schema"), list)
    ):
        return {
            "callable": False,
            "reason": "the cloud's definition does not match the uploaded workflow",
            "code": None,
        }
    return {"callable": True, "definition": body}


def curl_examples(endpoint: str, definition: Optional[Mapping[str, Any]]) -> list[str]:
    """Shell examples that reference the key by variable name only."""
    auth = '-H "Authorization: Bearer $OPENADAPT_API_KEY"'
    if definition is None:
        return []
    binding = definition["binding"]
    fields = (
        "schema_version",
        "qualification_id",
        "workflow_version",
        "workflow_digest",
        "environment_id",
        "effect_strength_schema_version",
        "minimum_effect_strength",
    )
    body: dict[str, Any] = {}
    for name in fields:
        value = binding.get(name)
        if not isinstance(value, str) or not _SHELL_SAFE.fullmatch(value):
            return []
        body[name] = value
    parameters: dict[str, str] = {}
    for row in definition["parameter_schema"]:
        label = row.get("name") if isinstance(row, dict) else None
        if not isinstance(label, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]{0,63}", label
        ):
            return []
        parameters[label] = f"<{label}>"
    body["parameters"] = parameters
    body["idempotency_key"] = "<unique-per-request>"
    body["authorization_context"] = {
        "actor_id": "<who-asked>",
        "authorization_reference": "<your-approval-reference>",
    }
    payload = json.dumps(body, sort_keys=True)
    return [
        f'curl -sS -X POST "{endpoint}" {auth} '
        f"-H \"Content-Type: application/json\" -d '{payload}'",
        f'curl -sS "{endpoint}?execution_id=<execution_id>" {auth}',
    ]


# -- the command -----------------------------------------------------------------


# -- the wrap run's own validation evidence -------------------------------------


@dataclass(frozen=True)
class WrapEvidence:
    """What the wrap run already produced that runtime validation needs."""

    #: The recording the program was compiled from (kept on this machine).
    recording: Path
    #: The attended canary run of the admitted program (``report.json``).
    run_dir: Path
    canary: Mapping[str, Any]


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve(strict=True).is_relative_to(root.resolve(strict=True))
    except OSError:
        return False


def _no_links(root: Path) -> bool:
    return not root.is_symlink() and not any(p.is_symlink() for p in root.rglob("*"))


def wrap_evidence(program: StagedProgram, *, key_dir: Optional[Path]) -> WrapEvidence:
    """Find the admitted program's recording and its signed, passing canary.

    Both are written by the wrap first run. Each is accepted only when it is
    signed with the local admission key for the admitted program's digest and
    lies inside the task's own cache directory.
    """
    from openadapt_flow.jit import lifecycle

    digest = program.admission.get("digest")
    program_dir = program.task_dir / "program"
    candidate = _read_json(program_dir / lifecycle.CANDIDATE_FILE)
    recorder = (candidate or {}).get("recorder")
    if (
        not digest
        or not isinstance(recorder, dict)
        or recorder.get("digest") != digest
        or not lifecycle._record_signed(recorder, key_dir)
    ):
        raise PublishError(
            "NO_WRAP_EVIDENCE",
            "the admitted program has no signed draft record (program/"
            "candidate.json) from its wrap run, so its recording is unknown. "
            "Run the task again with `openadapt wrap`",
        )
    recording: Optional[Path] = None
    for field in ("compiled_recording", "recording"):
        value = (candidate or {}).get(field)
        if isinstance(value, str) and value not in ("", "None"):
            path = Path(value)
            if path.is_dir() and _inside(path, program_dir) and _no_links(path):
                recording = path
                break
    if recording is None:
        raise PublishError(
            "NO_WRAP_EVIDENCE",
            "the recording this program was compiled from is missing from the "
            "task's cache directory. Run the task again with `openadapt wrap`",
        )

    ran_at = (program.admission.get("canary") or {}).get("ran_at")
    rows: list[dict[str, Any]] = []
    latest = _read_json(program_dir / lifecycle.CANARY_FILE)
    if latest is not None:
        rows.append(latest)
    history = program_dir / lifecycle.CANARIES_FILE
    if history.is_file() and not history.is_symlink():
        for line in reversed(history.read_text(encoding="utf-8").splitlines()):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    runs = program.task_dir / "runs"
    for row in rows:
        run_dir = row.get("run_dir")
        if (
            row.get("digest") != digest
            or row.get("passed") is not True
            or (ran_at is not None and row.get("ran_at") != ran_at)
            or not isinstance(run_dir, str)
            or not lifecycle._record_signed(row, key_dir)
        ):
            continue
        path = Path(run_dir)
        report = path / "report.json"
        if _inside(path, runs) and report.is_file() and not report.is_symlink():
            return WrapEvidence(recording=recording, run_dir=path, canary=row)
    raise PublishError(
        "NO_WRAP_EVIDENCE",
        "the admitted program has no signed, passing canary run in the task's "
        "cache directory. Run the task again with `openadapt wrap` and attend "
        "its canary",
    )


def _report_kind(run_dir: Path) -> Optional[str]:
    report = _read_json(run_dir / "report.json") or {}
    kind = report.get("execution_target_kind")
    return kind if isinstance(kind, str) else None


def _check_public_target(url: str) -> str:
    """The cloud runner opens the app itself: it must be public HTTPS."""
    from openadapt_flow.runtime_validation import (
        RuntimeValidationError,
        normalize_execution_scope,
    )

    try:
        return str(normalize_execution_scope(url, None)["entry_url"])
    except RuntimeValidationError:
        raise PublishError(
            "TARGET_NOT_PUBLIC",
            "the task's app URL is not a public HTTPS address, so OpenAdapt Cloud "
            "cannot open it (local, private and plain-HTTP addresses are refused). "
            "Wrap the task against a public HTTPS deployment of the app",
            exit_code=EXIT_SETUP,
        ) from None


# -- one review of what will be uploaded ----------------------------------------


def _derive(source: Path, destination: Path, kind: str) -> dict[str, Any]:
    """The sanitized derivative of ``source``, reused while ``source`` is unchanged."""
    from openadapt_flow import safe_errors
    from openadapt_flow.sanitized_artifact import (
        SanitizationError,
        load_and_verify_derivative,
        sanitize_artifact,
        source_tree_sha256,
    )

    if destination.is_symlink():
        raise PublishError("BUNDLE_UNSAFE", "the publish directory is a symbolic link")
    label = "program" if kind == "bundle" else "recording"
    try:
        if destination.exists():
            try:
                manifest = load_and_verify_derivative(destination)
                if manifest.get("source_tree_sha256") == source_tree_sha256(source):
                    return manifest
            except SanitizationError:
                pass
            shutil.rmtree(destination)
        return sanitize_artifact(source, destination, kind=kind)
    except SanitizationError as exc:
        raise PublishError(
            "SANITIZATION_FAILED",
            f"the {label} could not be sanitized: {safe_errors.message(exc)}",
        ) from None


def _approved(derivative: Path) -> bool:
    from openadapt_flow.sanitized_artifact import SanitizationError, load_valid_approval

    try:
        load_valid_approval(derivative)
    except (SanitizationError, OSError, ValueError):
        return False
    return True


def review_summary(
    program: StagedProgram,
    bundle_manifest: Mapping[str, Any],
    bundle_dir: Path,
    *,
    host: str,
    name: str,
    workflow_id: Optional[str],
    parameters: Sequence[str],
    target: Optional[str],
    canary: Optional[Mapping[str, Any]],
    org_id: Optional[str] = None,
) -> list[str]:
    """A short summary of exactly what will leave the machine."""
    files = list(bundle_manifest.get("files") or [])
    changed = sum(1 for row in files if isinstance(row, dict) and row.get("changed"))
    unresolved = len(bundle_manifest.get("residual_findings") or [])
    size = sum(p.stat().st_size for p in bundle_dir.rglob("*") if p.is_file())
    destination = f"new version of {workflow_id}" if workflow_id else "new workflow"
    lines = [
        f"Ready to publish to {host}:",
        *([f"  org         {org_id}"] if org_id else []),
        f"  workflow    {name} ({destination})",
        f"  program     {program.manifest['saves']} saving step(s), each read back "
        "by the task's verify.yaml; inputs: " + ", ".join(parameters),
        f"  upload      the sanitized program: {len(files)} files, "
        f"{max(1, round(size / 1024))} KB, {changed} changed by the scrubber, "
        f"{unresolved} unresolved findings",
    ]
    if canary is not None:
        lines += [
            "  recording   stays on this machine; only the hash of its sanitized "
            "copy is sent",
            f"  validation  the attended canary ({canary.get('outcome')}) on "
            f"{target or 'the task app'}",
        ]
    lines += [
        "  admission   this machine's admission public key is registered with "
        "the organization (once), so the cloud can check the signature",
        f"  inspect     {bundle_dir}",
    ]
    return lines


def _reviewer(args: argparse.Namespace) -> str:
    if args.reviewer:
        return str(args.reviewer)
    try:
        return f"{getpass.getuser()} (openadapt wrap publish)"
    except Exception:  # noqa: BLE001 - no login name in this environment
        return "local operator (openadapt wrap publish)"


def _confirm(args: argparse.Namespace, lines: Sequence[str]) -> Optional[bool]:
    """True to upload, False when declined, None when nobody can be asked."""
    if args.yes:
        return True
    if args.json or not sys.stdin.isatty():
        return None
    for line in lines:
        print(line)
    try:
        answer = input("Upload it? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


# -- the command -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openadapt wrap publish",
        allow_abbrev=False,
        description=(
            "Publish this task's admitted program as an OpenAdapt Cloud "
            "endpoint. Needs OPENADAPT_API_KEY. Shows what will be uploaded "
            "and asks once."
        ),
    )
    parser.add_argument("--task", required=True, help="The task file used by wrap")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the upload without asking (for scripts; the summary is "
        "still printed)",
    )
    parser.add_argument("--reviewer", help="Who confirmed the upload (default: you)")
    parser.add_argument("--key-dir", help="Admission key directory (default: local)")
    parser.add_argument("--name", help="Workflow name in the dashboard")
    parser.add_argument(
        "--host", help="Cloud base URL (default: configured host or app.openadapt.ai)"
    )
    parser.add_argument(
        "--workflow-id", help="Publish a new version of this existing workflow"
    )
    parser.add_argument(
        "--org",
        help="Organization id (default: $OPENADAPT_ORG_ID, else the API key's "
        "organization as the cloud reports it)",
    )
    parser.add_argument(
        "--validation-attestation",
        help="Use this `openadapt-flow validate-hosted` attestation instead of "
        "attesting the wrap run's canary",
    )
    parser.add_argument("--json", action="store_true", help="Print one JSON object")
    return parser


def _emit(args: argparse.Namespace, document: dict[str, Any], lines: list[str]) -> None:
    if args.json:
        print(json.dumps(document, sort_keys=True))
    else:
        for line in lines:
            print(line)


def _refuse(args: argparse.Namespace, error: PublishError) -> int:
    if args.json:
        print(
            json.dumps(
                {"status": "refused", "code": error.code, "reason": error.message}
            )
        )
    else:
        print(
            f"openadapt wrap publish: {error.message} [{error.code}]", file=sys.stderr
        )
    return error.exit_code


def _write_record(record_dir: Path, document: Mapping[str, Any]) -> None:
    from openadapt_flow.record_sor import _write_private

    target = record_dir / "published.json"
    if target.exists():
        target.unlink()
    _write_private(target, json.dumps(document, indent=2, sort_keys=True) + "\n")


def _no_key(host: str) -> PublishError:
    return PublishError(
        "NO_API_KEY",
        "OPENADAPT_API_KEY is not set. Create a key at "
        f"{host}/dashboard/settings/api-keys and export it",
        exit_code=EXIT_SETUP,
    )


def _key_rejected() -> PublishError:
    return PublishError(
        "API_KEY_REJECTED",
        "the cloud rejected OPENADAPT_API_KEY. Check that the key is current and "
        "belongs to the organization you are publishing to",
        exit_code=EXIT_SETUP,
    )


def _upload_error(exc: Exception) -> PublishError:
    from openadapt_flow import safe_errors

    text = safe_errors.message(exc)
    if "(401)" in text:
        return _key_rejected()
    if re.search(r"returned 404\b", text):
        return PublishError(
            "WORKFLOW_API_UNAVAILABLE",
            "the workflow publish route was not found (404). Check that the "
            "workflow API is enabled for the organization and the workflow id",
            exit_code=EXIT_SETUP,
        )
    if "runtime_validation_rejected" in text:
        return PublishError(
            "VALIDATION_REJECTED",
            "the cloud refused the program's runtime validation "
            "(runtime_validation_rejected). Nothing was published",
        )
    return PublishError("UPLOAD_REFUSED", "the governed upload was refused: " + text)


def _attest(
    evidence: WrapEvidence,
    recording_derivative: Path,
    bundle_derivative: Path,
    *,
    policy: str,
    environment: str,
    target: Optional[str],
    host: str,
    key: str,
    org_id: str,
) -> dict[str, Any]:
    from openadapt_flow import safe_errors
    from openadapt_flow.runtime_validation import (
        RuntimeValidationError,
        create_admitted_program_attestation,
        request_validation_challenge,
    )

    try:
        # The challenge comes from the organization's API-key route, so the
        # attestation MAC'd with the same key is accepted by its publish route.
        challenge = request_validation_challenge(
            host=host,
            token=key,
            path=f"/api/v1/org/{org_id}/validation-challenges",
        )
        return create_admitted_program_attestation(
            recording_source=evidence.recording,
            recording_derivative=recording_derivative,
            bundle_derivative=bundle_derivative,
            run_dir=evidence.run_dir,
            policy_source=policy,
            environment=environment,
            target_url=target,
            host=host,
            token=key,
            challenge=challenge,
        )
    except RuntimeValidationError as exc:
        text = safe_errors.message(exc)
        if "(401)" in text:
            raise _key_rejected() from None
        if re.search(r"returned 404\b", text):
            raise _workflow_api_unavailable(host) from None
        raise PublishError(
            "VALIDATION_FAILED",
            "the program did not pass runtime validation, so nothing was "
            f"uploaded: {text}",
        ) from None


def run(args: argparse.Namespace) -> int:
    from openadapt_flow import hosted
    from openadapt_flow.integrations.agents.recording import TaskSetupError, load_task

    key: Optional[str] = None
    try:
        try:
            setup = load_task(args.task, key_dir=args.key_dir)
        except TaskSetupError as exc:
            raise PublishError(
                "TASK_INVALID",
                f"the task file is not a valid wrap task ({exc})",
                exit_code=EXIT_SETUP,
            ) from None
        host = hosted.resolve_host(args.host)
        key = api_key()
        if key is None:
            raise _no_key(host)
        key_dir = Path(args.key_dir).expanduser() if args.key_dir else None
        program = stage(
            setup.cache, setup.instruction, setup.parameters, key_dir=key_dir
        )
        common = getattr(setup.cache, "common", {}) or {}
        name = args.name or setup.cache.task_id(
            setup.instruction, dict.fromkeys(setup.parameters, "")
        )
        evidence: Optional[WrapEvidence] = None
        target: Optional[str] = None
        if not args.validation_attestation:
            evidence = wrap_evidence(program, key_dir=key_dir)
            if _report_kind(evidence.run_dir) == "web":
                target = _check_public_target(str(common.get("url", "")))
        public_key, fingerprint = admission_public_key(key_dir)
        # Read-only: which organization this key publishes to. Nothing about
        # the program is sent before the confirmation below.
        org_id = resolve_org(host, key, args.org)
        route = upload_route(org_id, args.workflow_id)

        bundle_derivative = program.record_dir / "sanitized-bundle"
        bundle_manifest = _derive(program.staged, bundle_derivative, "bundle")
        if bundle_manifest.get("execution_semantics") != "preserved":
            raise PublishError(
                "SANITIZATION_CHANGED_PROGRAM",
                "the scrubber had to change the program's own files, so the upload "
                "would not be the admitted program. Keep sensitive values out of "
                "the task's constants and on-screen text, then run wrap again",
            )
        derivatives = [(bundle_derivative, program.staged)]
        recording_derivative = program.record_dir / "sanitized-recording"
        if evidence is not None:
            _derive(evidence.recording, recording_derivative, "recording")
            derivatives.append((recording_derivative, evidence.recording))

        summary = review_summary(
            program,
            bundle_manifest,
            bundle_derivative,
            host=host,
            name=name,
            workflow_id=args.workflow_id,
            parameters=setup.parameters,
            target=target,
            canary=evidence.canary if evidence is not None else None,
            org_id=org_id,
        )
        if not all(_approved(path) for path, _source in derivatives):
            answer: Optional[bool]
            automatic = hosted._auto_approve_enabled(None)
            answer = True if automatic else _confirm(args, summary)
            if not answer:
                return _waiting_confirm(args, program, summary, asked=answer is False)
            _approve(derivatives, args, automatic=automatic and not args.yes)
            summary_shown = automatic or args.yes or args.json
        else:
            summary_shown = False
        if summary_shown and not args.json:
            for line in summary:
                print(line)

        register_admission_key(host, key, org_id, public_key, fingerprint)
        attestation: Any = args.validation_attestation
        if evidence is not None:
            attestation = _attest(
                evidence,
                recording_derivative,
                bundle_derivative,
                policy=str(common.get("policy", "clinical-write")),
                environment=str(
                    getattr(setup.cache, "environment", "")
                    or (evidence.canary.get("environment") or "")
                    or (target or "wrap")
                ),
                target=target,
                host=host,
                key=key,
                org_id=org_id,
            )
        result = hosted.push(
            bundle_derivative,
            kind="bundle",
            name=name,
            host=host,
            token=key,
            workflow_id=args.workflow_id,
            validation_attestation=attestation,
            route=route,
        )
    except PublishError as error:
        return _refuse(args, error)
    except hosted.HostedDeliveryUncertain:
        return _refuse(
            args,
            PublishError(
                "DELIVERY_UNCERTAIN",
                "the upload may have reached the cloud without a confirmed answer. "
                "Do not run publish again; reconcile the artifact in the dashboard",
            ),
        )
    except hosted.HostedError as exc:
        return _refuse(args, _upload_error(exc))
    try:
        return _finish(args, program, result, host, key, org_id)
    finally:
        del key


def _approve(
    derivatives: Sequence[tuple[Path, Path]],
    args: argparse.Namespace,
    *,
    automatic: bool,
) -> None:
    from openadapt_flow import safe_errors
    from openadapt_flow.sanitized_artifact import SanitizationError, approve_derivative

    reviewer = "policy:complete-type-coverage" if automatic else _reviewer(args)
    try:
        for derivative, source in derivatives:
            if not _approved(derivative):
                approve_derivative(
                    derivative, source=source, reviewer=reviewer, automatic=automatic
                )
    except SanitizationError as exc:
        raise PublishError(
            "SANITIZATION_FAILED",
            f"the sanitized copy could not be approved: {safe_errors.message(exc)}",
        ) from None


def _waiting_confirm(
    args: argparse.Namespace,
    program: StagedProgram,
    summary: list[str],
    *,
    asked: bool,
) -> int:
    hint = (
        "Nothing was uploaded."
        if asked
        else "Nothing was uploaded. Run this command again with --yes to confirm "
        "the upload, or run it at a terminal to be asked."
    )
    document = {
        "status": "waiting",
        "step": "confirm_upload",
        "staged": str(program.staged),
        "summary": summary,
        "next": "rerun with --yes",
    }
    _emit(args, document, ([] if asked else summary) + [hint])
    return EXIT_WAITING


def _finish(
    args: argparse.Namespace,
    program: StagedProgram,
    result: Mapping[str, Any],
    host: str,
    key: Optional[str],
    expected_org: str,
) -> int:
    version = result.get("version")
    workflow_id = result.get("workflow_id")
    if not isinstance(version, dict) or not isinstance(workflow_id, str):
        return _refuse(
            args,
            PublishError(
                "CLOUD_RESPONSE_INVALID",
                "the cloud accepted the upload without a workflow version",
            ),
        )
    org_id = str(version.get("org_id", ""))
    # The acknowledgment names the endpoint path and the qualification the
    # definition and Execute requests bind to; both must agree with the
    # retained version, in the organization the upload went to.
    qualification_id = result.get("qualification_id")
    execute_path = result.get("execute_path")
    retained_qualification = version.get("runtime_validation_id")
    if (
        org_id != expected_org
        or not isinstance(qualification_id, str)
        or not _UUID.fullmatch(qualification_id)
        or (
            retained_qualification is not None
            and retained_qualification != qualification_id
        )
        or not isinstance(execute_path, str)
    ):
        return _refuse(
            args,
            PublishError(
                "CLOUD_RESPONSE_INVALID",
                "the cloud's acknowledgment does not name this organization's "
                "endpoint and qualification",
            ),
        )
    try:
        endpoint = endpoint_url(host, org_id, workflow_id)
    except PublishError as error:
        return _refuse(args, error)
    if execute_path != endpoint[len(host.rstrip("/")) :]:
        return _refuse(
            args,
            PublishError(
                "CLOUD_RESPONSE_INVALID",
                "the cloud's endpoint path does not match the uploaded workflow",
            ),
        )
    if key is None:
        check: dict[str, Any] = {
            "callable": None,
            "reason": "set OPENADAPT_API_KEY to check that the endpoint is callable",
            "code": None,
        }
    else:
        check = fetch_definition(
            host,
            key,
            org_id=org_id,
            workflow_id=workflow_id,
            qualification_id=qualification_id,
        )
    definition = check.get("definition") if check.get("callable") else None
    examples = curl_examples(endpoint, definition)
    definition_curl = (
        f'curl -sS "{endpoint}/definition?qualification_id={qualification_id}" '
        '-H "Authorization: Bearer $OPENADAPT_API_KEY"'
    )
    document = {
        "status": "published",
        "callable": check.get("callable"),
        "reason": check.get("reason"),
        "code": check.get("code"),
        "endpoint": endpoint,
        "definition_url": f"{endpoint}/definition?qualification_id={qualification_id}",
        "org_id": org_id,
        "workflow_id": workflow_id,
        "qualification_id": qualification_id,
        "workflow_version": version.get("id"),
        "version": version.get("version"),
        "dashboard_url": result.get("dashboard_url"),
        "readback_sha256": program.manifest["readback"]["sha256"],
        "record_check_sha256": program.manifest["record_check_sha256"],
        "examples": examples or [definition_curl],
    }
    _write_record(program.record_dir, document)
    lines = [
        f"Published. Endpoint: {endpoint}",
        f"Dashboard: {result.get('dashboard_url')}",
    ]
    if check.get("callable"):
        lines.append(
            "The endpoint is callable. Each call returns an execution id; its "
            "status carries the verified receipt (VERIFIED, HALTED or UNCERTAIN)."
        )
        lines += ["Call it:", *(f"  {line}" for line in examples)]
    else:
        suffix = f" ({check['code']})" if check.get("code") else ""
        lines.append(f"Not callable yet: {check.get('reason')}{suffix}.")
        lines += ["Check it:", f"  {definition_curl}"]
    _emit(args, document, lines)
    if check.get("callable") is False:
        return EXIT_WAITING
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except Exception:  # noqa: BLE001 - never reflect values or credentials
        print(
            "openadapt wrap publish: local setup or upload failed unexpectedly",
            file=sys.stderr,
        )
        return EXIT_SETUP
