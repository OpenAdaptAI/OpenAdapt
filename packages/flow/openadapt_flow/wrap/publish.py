"""Publish a wrapped task's admitted program as an OpenAdapt Cloud endpoint.

``openadapt wrap publish --task TASK.json`` ships the program that
``openadapt wrap`` already compiled, checked and admitted for that task. It
adds no new way to reach the cloud: the upload is the governed bundle
``push`` (local sanitization, a person's review, a runtime-validation
attestation, exact-hash archive), and the endpoint is the cloud's existing
workflow Execute API. What this module adds is the part between them:

* it finds the task's admitted program from the task file alone,
* it refuses a program the cloud could not read back: no saving step, a
  saving step with no effect contract, or no confirmed ``verify.yaml``,
* it re-verifies the admission signature before anything leaves the machine,
* it stages the bundle with its read-back configuration and a small manifest,
* after the upload it asks the cloud for the endpoint's current definition
  with the caller's API key, and prints the URL and a ``curl`` example.

Every step that needs a person or a credential stops with the exact next
command. Running the same command again resumes from where it stopped. No
credential is printed, logged or written to the publish record.
"""

from __future__ import annotations

import argparse
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
        verify_admission(bundle, key_dir=key_dir)
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
    return StagedProgram(staged=staged, manifest=manifest, record_dir=record_dir)


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openadapt wrap publish",
        allow_abbrev=False,
        description=(
            "Publish this task's admitted program as an OpenAdapt Cloud "
            "endpoint. Run it again after each step it asks for."
        ),
    )
    parser.add_argument("--task", required=True, help="The task file used by wrap")
    parser.add_argument("--key-dir", help="Admission key directory (default: local)")
    parser.add_argument("--name", help="Workflow name in the dashboard")
    parser.add_argument(
        "--host", help="Cloud base URL (default: configured host or app.openadapt.ai)"
    )
    parser.add_argument(
        "--validation-attestation",
        help="Runtime-validation attestation from `openadapt-flow validate-hosted`",
    )
    parser.add_argument(
        "--workflow-id", help="Publish a new version of this existing workflow"
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


def _approved(derivative: Path) -> bool:
    from openadapt_flow.sanitized_artifact import SanitizationError, load_valid_approval

    try:
        load_valid_approval(derivative)
    except (SanitizationError, OSError, ValueError):
        return False
    return True


def _write_record(record_dir: Path, document: Mapping[str, Any]) -> None:
    from openadapt_flow.record_sor import _write_private

    target = record_dir / "published.json"
    if target.exists():
        target.unlink()
    _write_private(target, json.dumps(document, indent=2, sort_keys=True) + "\n")


def run(args: argparse.Namespace) -> int:
    from openadapt_flow import hosted
    from openadapt_flow.integrations.agents.recording import TaskSetupError, load_task

    try:
        try:
            setup = load_task(args.task, key_dir=args.key_dir)
        except TaskSetupError as exc:
            raise PublishError(
                "TASK_INVALID",
                f"the task file is not a valid wrap task ({exc})",
                exit_code=EXIT_SETUP,
            ) from None
        key_dir = Path(args.key_dir).expanduser() if args.key_dir else None
        program = stage(
            setup.cache, setup.instruction, setup.parameters, key_dir=key_dir
        )
        try:
            host = hosted.resolve_host(args.host)
            hosted.resolve_token(host=host)
        except hosted.HostedError:
            raise PublishError(
                "NO_INGEST_TOKEN",
                "no OpenAdapt ingest token for the upload. Set "
                "OPENADAPT_INGEST_TOKEN or run `openadapt-flow login` (mint one at "
                "<host>/dashboard/settings/ingest)",
                exit_code=EXIT_SETUP,
            ) from None
        name = args.name or setup.cache.task_id(
            setup.instruction, dict.fromkeys(setup.parameters, "")
        )
        common: dict[str, Any] = dict(
            kind="bundle",
            name=name,
            host=host,
            workflow_id=args.workflow_id,
            validation_attestation=args.validation_attestation,
        )
        # A raw staged bundle is sanitized locally and pauses for review
        # before push reads the attestation or opens a connection. Under an
        # administrator's auto-approval policy push continues on its own.
        result = hosted.push(program.staged, **common)
        if result.get("pending_review"):
            derivative = Path(result["sanitized_path"])
            if not _approved(derivative):
                return _waiting_review(args, program, derivative)
            if not args.validation_attestation:
                return _waiting_validation(args, program, derivative)
            result = hosted.push(derivative, **common)
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
        from openadapt_flow import safe_errors

        if not args.validation_attestation and "runtime-validation attestation" in str(
            exc
        ):
            return _waiting_validation(args, program, None)
        return _refuse(
            args,
            PublishError(
                "UPLOAD_REFUSED",
                "the governed upload was refused: " + safe_errors.message(exc),
            ),
        )
    return _finish(args, program, result, host)


def _waiting_review(
    args: argparse.Namespace,
    program: StagedProgram,
    derivative: Path,
) -> int:
    review = f"openadapt-flow review-sanitized {derivative} --original {program.staged}"
    approve = (
        f"openadapt-flow approve-sanitized {derivative} --original {program.staged} "
        "--reviewer <you>"
    )
    document = {
        "status": "waiting",
        "step": "review_sanitized",
        "staged": str(program.staged),
        "sanitized_path": str(derivative),
        "next": [review, approve],
    }
    _emit(
        args,
        document,
        [
            "Program checked: admitted, with a read-back check on every save.",
            f"Staged for upload at {program.staged}.",
            f"A sanitized copy is waiting for your review at {derivative}.",
            "Nothing has been uploaded. Next:",
            f"  {review}",
            f"  {approve}",
            "Then run this publish command again.",
        ],
    )
    return EXIT_WAITING


def _waiting_validation(
    args: argparse.Namespace, program: StagedProgram, derivative: Optional[Path]
) -> int:
    bundle = derivative if derivative is not None else "<approved bundle derivative>"
    out = program.record_dir / "validation-attestation.json"
    command = (
        f"openadapt-flow validate-hosted --bundle {bundle} "
        "--recording <approved recording derivative> --run-dir <governed replay run> "
        "--policy <policy> --risk-class <low|consequential> --environment <name> "
        f"--target-url <https entry url> --out {out}"
    )
    document = {
        "status": "waiting",
        "step": "validate_hosted",
        "staged": str(program.staged),
        "sanitized_path": str(derivative) if derivative is not None else None,
        "next": [command],
    }
    _emit(
        args,
        document,
        [
            "The sanitized program is approved. The cloud also needs a "
            "runtime-validation attestation before it will run it:",
            f"  {command}",
            f"Then run this publish command again with --validation-attestation {out}.",
        ],
    )
    return EXIT_WAITING


def _finish(
    args: argparse.Namespace,
    program: StagedProgram,
    result: Mapping[str, Any],
    host: str,
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
    qualification_id = str(version.get("runtime_validation_id", ""))
    try:
        endpoint = endpoint_url(host, org_id, workflow_id)
    except PublishError as error:
        return _refuse(args, error)
    key = api_key()
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
    del key
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
