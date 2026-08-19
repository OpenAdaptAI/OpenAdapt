"""flow_bridge -- thin wrapper around the ``openadapt-flow`` CLI.

The desktop engine does NOT reimplement the loop; it shells out to the
``openadapt-flow`` package for ``record / compile / replay / run / teach``
(spec section 1.1: WRAP, do not touch its internals). This module centralizes
that invocation so the controller, the CLI loop verbs, and the IPC handlers all
speak to flow the same way.

Halt signaling reconciliation (spec section 8, item 3): ``replay``/``run`` exit
0/1 (never 2), so a halt is read from ``report.json`` (``halt`` /
``HaltObservation``), NOT from the exit code. :meth:`FlowBridge.read_report` and
:meth:`FlowBridge.read_halt` implement that.

Bundle-dir-vs-zip reconciliation (item 4) lives in :mod:`engine.hosted`, which
zips a bundle/recording directory before upload.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Callable, Literal, cast

from loguru import logger

FLOW_BIN = "openadapt-flow"
EMBEDDED_FLOW_MODE = "__openadapt_flow__"
EMBEDDED_FLOW_RECORD_MODE = "__openadapt_flow_record_config__"
FlowOutcome = Literal[
    "VERIFIED",
    "COMPLETED_UNVERIFIED",
    "HALTED",
    "FAILED",
    "ROLLED_BACK",
    "success",
    "halt",
    "unknown",
]
PRECISE_FLOW_OUTCOMES = frozenset(
    {
        "VERIFIED",
        "COMPLETED_UNVERIFIED",
        "HALTED",
        "FAILED",
        "ROLLED_BACK",
    }
)
_OUTCOME_PROFILES = frozenset({"demo", "standard", "regulated"})
_OUTCOME_CONTRACT_KEYS = frozenset({"authorization", "identity", "postcondition", "effect"})
_OUTCOME_EVIDENCE_CLASSES = frozenset(
    {
        "authorization",
        "identity",
        "postcondition",
        "effect_tier_1",
        "effect_tier_2",
        "effect_tier_3",
        "effect_tier_4",
        "model",
        "compensation",
    }
)
_OUTCOME_ENVELOPE_REQUIRED_KEYS = frozenset(
    {
        "version",
        "outcome",
        "profile",
        "production_eligible",
        "execution_completed",
        "required_contracts",
        "passed_contracts",
        "evidence_classes",
        "model_calls",
        "external_network_calls",
        "compensation_actions",
    }
)
_OUTCOME_ENVELOPE_OPTIONAL_KEYS = frozenset(
    {
        "qualification_evidence_only",
        "workflow_contract_sha256",
        "postcondition_evidence",
    }
)
_POSTCONDITION_EVIDENCE_KEYS = frozenset(
    {
        "result_index",
        "workflow_contract_sha256",
        "step_index",
        "step_contract_sha256",
        "action_kind",
        "actuation_path",
        "contract_kind",
        "contract_index",
        "contract_sha256",
        "verdict",
    }
)
_POSTCONDITION_ACTION_KINDS = frozenset(
    {
        "click",
        "double_click",
        "right_click",
        "drag",
        "type",
        "select_option",
        "key",
        "hotkey",
        "wait",
        "scroll",
    }
)
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_INGEST_TOKEN_ENV = "OPENADAPT_INGEST_TOKEN"


def _contract_counts(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict) or set(value) != _OUTCOME_CONTRACT_KEYS:
        return None
    if any(
        not isinstance(count, int) or isinstance(count, bool) or count < 0
        for count in value.values()
    ):
        return None
    return cast(dict[str, int], value)


def _valid_precise_envelope(report: dict, envelope: dict, outcome: object) -> bool:
    """Mirror Flow's v1 envelope invariants at the Desktop trust boundary."""

    envelope_keys = set(envelope)
    if not _OUTCOME_ENVELOPE_REQUIRED_KEYS <= envelope_keys or not envelope_keys <= (
        _OUTCOME_ENVELOPE_REQUIRED_KEYS | _OUTCOME_ENVELOPE_OPTIONAL_KEYS
    ):
        return False
    profile = envelope.get("profile")
    if profile is not None and profile not in _OUTCOME_PROFILES:
        return False
    production = envelope.get("production_eligible")
    completed = envelope.get("execution_completed")
    if not isinstance(production, bool) or not isinstance(completed, bool):
        return False
    required = _contract_counts(envelope.get("required_contracts"))
    passed = _contract_counts(envelope.get("passed_contracts"))
    if required is None or passed is None:
        return False
    if any(passed[key] > required[key] for key in _OUTCOME_CONTRACT_KEYS):
        return False
    qualification_only = envelope.get("qualification_evidence_only", False)
    if not isinstance(qualification_only, bool):
        return False
    workflow_digest = envelope.get("workflow_contract_sha256")
    if workflow_digest is not None and (
        not isinstance(workflow_digest, str) or not _SHA256_RE.fullmatch(workflow_digest)
    ):
        return False
    postcondition_evidence = envelope.get("postcondition_evidence", [])
    if (
        not isinstance(postcondition_evidence, list)
        or len(postcondition_evidence) != required["postcondition"]
    ):
        return False
    if postcondition_evidence and workflow_digest is None:
        return False
    evidence_keys: list[tuple[int, str, int]] = []
    result_contracts: list[tuple[int, str]] = []
    passed_postconditions = 0
    for item in postcondition_evidence:
        if not isinstance(item, dict) or set(item) != _POSTCONDITION_EVIDENCE_KEYS:
            return False
        integer_fields = ("result_index", "step_index", "contract_index")
        if any(
            not isinstance(item.get(field), int)
            or isinstance(item.get(field), bool)
            or item[field] < 0
            for field in integer_fields
        ):
            return False
        if (
            item.get("workflow_contract_sha256") != workflow_digest
            or item.get("action_kind") not in _POSTCONDITION_ACTION_KINDS
            or item.get("actuation_path") != "gui"
            or item.get("contract_kind") not in {"explicit_predicate", "intrinsic_input_readback"}
            or item.get("verdict") not in {"passed", "refuted", "unverifiable"}
        ):
            return False
        if item["contract_kind"] == "intrinsic_input_readback" and (
            item["action_kind"] not in {"type", "select_option"} or item["contract_index"] != 0
        ):
            return False
        if any(
            not isinstance(item.get(field), str) or not _SHA256_RE.fullmatch(item[field])
            for field in ("step_contract_sha256", "contract_sha256")
        ):
            return False
        expected_step = hashlib.sha256(
            json.dumps(
                {
                    "domain": "openadapt.postcondition-step/v1",
                    "workflow_contract_sha256": workflow_digest,
                    "step_index": item["step_index"],
                    "action_kind": item["action_kind"],
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        if item["step_contract_sha256"] != expected_step:
            return False
        expected_contract = hashlib.sha256(
            json.dumps(
                {
                    "domain": "openadapt.postcondition-contract/v1",
                    "workflow_contract_sha256": workflow_digest,
                    "step_contract_sha256": expected_step,
                    "action_kind": item["action_kind"],
                    "actuation_path": "gui",
                    "contract_kind": item["contract_kind"],
                    "contract_index": item["contract_index"],
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        if item["contract_sha256"] != expected_contract:
            return False
        evidence_keys.append((item["result_index"], item["contract_kind"], item["contract_index"]))
        result_contracts.append((item["result_index"], item["contract_sha256"]))
        passed_postconditions += item["verdict"] == "passed"
    if (
        len(evidence_keys) != len(set(evidence_keys))
        or len(result_contracts) != len(set(result_contracts))
        or passed_postconditions != passed["postcondition"]
    ):
        return False
    evidence = envelope.get("evidence_classes")
    if (
        not isinstance(evidence, list)
        or any(
            not isinstance(item, str) or item not in _OUTCOME_EVIDENCE_CLASSES for item in evidence
        )
        or len(evidence) != len(set(evidence))
    ):
        return False
    if ("postcondition" in evidence) != bool(passed["postcondition"]):
        return False
    model_calls = envelope.get("model_calls")
    compensation_actions = envelope.get("compensation_actions")
    if (
        not isinstance(model_calls, int)
        or isinstance(model_calls, bool)
        or model_calls < 0
        or not isinstance(compensation_actions, int)
        or isinstance(compensation_actions, bool)
        or compensation_actions < 0
        or envelope.get("external_network_calls") not in {"none", "observed", "unknown"}
    ):
        return False
    if "external_network_calls" in report and (
        report.get("external_network_calls") not in {"none", "observed", "unknown"}
        or report.get("external_network_calls") != envelope.get("external_network_calls")
    ):
        return False
    if outcome in {"VERIFIED", "COMPLETED_UNVERIFIED"} and completed is not True:
        return False
    if outcome == "VERIFIED":
        if (
            required != passed
            or profile not in {"standard", "regulated"}
            or not (production is True or qualification_only is True)
            or required["authorization"] < 1
        ):
            return False
    elif production is not False:
        return False
    if production and qualification_only:
        return False
    has_compensation = "compensation" in evidence
    if has_compensation != (compensation_actions > 0):
        return False
    if (outcome == "ROLLED_BACK") != (compensation_actions > 0):
        return False
    return (
        isinstance(report.get("success"), bool)
        and report.get("execution_profile") == profile
        and report.get("production_eligible") == production
        and isinstance(report.get("production_eligible"), bool)
        and report.get("execution_completed") == completed
        and isinstance(report.get("execution_completed"), bool)
        and report.get("model_calls") == model_calls
    )


class FlowNotAvailableError(RuntimeError):
    """Raised when the ``openadapt-flow`` CLI cannot be located."""


class BrowserRuntimeError(RuntimeError):
    """Raised when the pinned browser runtime cannot be made available."""


BrowserProgress = Callable[[str, str], None]


@dataclass
class FlowResult:
    """Result of a flow subprocess invocation.

    Attributes:
        ok: True when the process exited 0.
        returncode: The process exit code.
        stdout: Captured standard output.
        stderr: Captured standard error.
        out_dir: The primary output directory (recording/bundle/run), if known.
    """

    ok: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""
    out_dir: Path | None = None


@dataclass
class FlowRecordingSession:
    """One canonical Flow record subprocess controlled by private sentinels."""

    process: subprocess.Popen[str]
    out_dir: Path
    stop_path: Path
    ready_path: Path

    @property
    def is_running(self) -> bool:
        return self.process.poll() is None

    def stop(self, timeout: float = 180) -> FlowResult:
        """Ask Flow to finalize normally, then collect its bounded output."""

        if self.process.poll() is None:
            self.stop_path.parent.mkdir(parents=True, exist_ok=True)
            self.stop_path.touch(mode=0o600, exist_ok=True)
        try:
            stdout, stderr = self.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # A recorder that cannot finalize must not be reported as a usable
            # capture. Termination is a last-resort resource cleanup.
            self.process.terminate()
            stdout, stderr = self.process.communicate(timeout=30)
        finally:
            self.stop_path.unlink(missing_ok=True)
            self.ready_path.unlink(missing_ok=True)
        return FlowResult(
            ok=self.process.returncode == 0,
            returncode=int(self.process.returncode or 0),
            stdout=stdout or "",
            stderr=stderr or "",
            out_dir=self.out_dir,
        )


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _flow_command(flow_bin: str = FLOW_BIN) -> list[str] | None:
    """Resolve Flow without ever trusting PATH in the installed application."""

    if flow_bin == FLOW_BIN and _is_frozen():
        return [sys.executable, EMBEDDED_FLOW_MODE]
    resolved = shutil.which(flow_bin)
    return [resolved] if resolved else None


def flow_available(flow_bin: str = FLOW_BIN) -> bool:
    """Whether the bundled or development ``openadapt-flow`` CLI is usable."""
    return _flow_command(flow_bin) is not None


def flow_runtime_source() -> str:
    """Human-readable source of the Flow runtime for diagnostics."""

    return "bundled with OpenAdapt Desktop" if _is_frozen() else "development PATH"


def _subprocess_env() -> dict[str, str]:
    """Environment for the isolated Flow process.

    Preserve ``PLAYWRIGHT_BROWSERS_PATH`` when an offline enterprise image
    provides a browser runtime.  Otherwise Flow's pinned Playwright package
    provisions its matching Chromium on first use; ``engine.main`` supports
    that package's normal ``sys.executable -m playwright`` call in a freeze.
    """

    return dict(os.environ)


def _safe_command_for_log(cmd: list[str]) -> str:
    """Render a command without logging secret- or selector-bearing values."""

    redacted_after = {
        "--token",
        "--host",
        "--password",
        "--rdp-password",
        "--agent-token",
        "--url",
        "--agent-url",
        "--macos-app",
        "--macos-window-title",
        "--linux-app",
        "--linux-window-title",
        "--rdp-host",
        "--rdp-window",
        "--rdp-window-title",
        "--rdp-readiness-text",
    }
    safe: list[str] = []
    redact_next = False
    egress_verb_index = next(
        (index for index, value in enumerate(cmd) if value in {"push", "report-break"}),
        None,
    )
    for index, value in enumerate(cmd):
        if redact_next:
            safe.append("[REDACTED]")
            redact_next = False
            continue
        if (
            egress_verb_index is not None
            and index == egress_verb_index + 1
            and not value.startswith("-")
        ):
            safe.append("[LOCAL_PATH]")
            continue
        safe.append(value)
        redact_next = value in redacted_after
    return " ".join(safe)


class FlowBridge:
    """Invokes the ``openadapt-flow`` CLI for the local loop steps.

    Args:
        flow_bin: The flow executable name/path.
        runner: Callable with ``subprocess.run`` semantics (injected in tests).
    """

    def __init__(
        self,
        flow_bin: str = FLOW_BIN,
        runner=subprocess.run,
        popen=subprocess.Popen,
    ) -> None:
        self.flow_bin = flow_bin
        self._runner = runner
        self._popen = popen
        self._run_auth_support: bool | None = None
        self._push_json_support: bool | None = None

    # --- low-level ---

    def _run(
        self,
        args: list[str],
        out_dir: Path | None = None,
        timeout: float | None = None,
        *,
        env_overrides: dict[str, str] | None = None,
    ) -> FlowResult:
        prefix = _flow_command(self.flow_bin)
        if prefix is None:
            raise FlowNotAvailableError(
                f"'{self.flow_bin}' not found on PATH; install openadapt-flow."
            )
        cmd = [*prefix, *args]
        logger.debug("flow: {cmd}", cmd=_safe_command_for_log(cmd))
        env = _subprocess_env()
        if env_overrides:
            env.update(env_overrides)
        proc = self._runner(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return FlowResult(
            ok=proc.returncode == 0,
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            out_dir=out_dir,
        )

    def browser_runtime_present(self) -> bool:
        """Whether the Chromium revision matching bundled Playwright exists."""

        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                executable = Path(playwright.chromium.executable_path)
            return executable.is_file()
        except Exception:
            return False

    def ensure_browser_runtime(
        self,
        progress: BrowserProgress | None = None,
        timeout: float = 900,
    ) -> None:
        """Provision pinned Chromium without requiring a terminal or Python.

        ``PLAYWRIGHT_BROWSERS_PATH`` is honored by Playwright, so an air-gapped
        deployment can point at a pre-provisioned, version-matched directory.
        The normal self-serve path downloads once into Playwright's per-user
        cache.  Failure is explicit and retryable; callers must not begin a run.
        """

        notify = progress or (lambda _state, _detail: None)
        notify("checking", "Checking the browser runtime…")
        if self.browser_runtime_present():
            notify("ready", "Browser runtime ready.")
            return

        notify(
            "installing",
            "Downloading the browser runtime once. You can keep using the app.",
        )
        command = [sys.executable, "-m", "playwright", "install", "chromium"]
        try:
            result = self._runner(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=_subprocess_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            notify("error", "Browser setup did not finish. Select Replay to retry.")
            raise BrowserRuntimeError(
                "Browser setup did not finish. Check your connection and select Replay "
                "to retry. Air-gapped deployments can set PLAYWRIGHT_BROWSERS_PATH."
            ) from exc
        if result.returncode != 0 or not self.browser_runtime_present():
            detail = (result.stderr or result.stdout or "").strip()[-1000:]
            notify("error", "Browser setup failed. Select Replay to retry.")
            suffix = f" Details: {detail}" if detail else ""
            raise BrowserRuntimeError(
                "Browser setup failed. Check your connection and select Replay to retry." + suffix
            )
        notify("ready", "Browser runtime ready.")

    # --- loop steps ---

    def record(
        self, out_dir: Path, url: str | None = None, timeout: float | None = None
    ) -> FlowResult:
        """Record a workflow into ``out_dir`` (a flow recording directory)."""
        args = ["record", "--out", str(out_dir)]
        if url:
            args += ["--url", url]
        return self._run(args, out_dir=out_dir, timeout=timeout)

    def demo_record(
        self,
        out_dir: Path,
        timeout: float | None = None,
    ) -> FlowResult:
        """Generate Flow's canonical bundled demonstration recording."""

        return self._run(
            ["demo-record", "--out", str(out_dir)],
            out_dir=out_dir,
            timeout=timeout,
        )

    def start_record(
        self,
        out_dir: Path,
        *,
        request: Path,
        stop_path: Path,
        ready_path: Path,
        startup_timeout: float = 15,
    ) -> FlowRecordingSession:
        """Start canonical Flow authoring with only a private path in argv."""

        if _is_frozen():
            command = [
                sys.executable,
                EMBEDDED_FLOW_RECORD_MODE,
                str(request),
                "--watch-parent-stdin",
            ]
        else:
            if find_spec("openadapt_flow") is None:
                raise FlowNotAvailableError(
                    "openadapt-flow is not importable by the Desktop runtime"
                )
            command = [
                sys.executable,
                "-m",
                "engine.flow_record_entry",
                str(request),
                "--watch-parent-stdin",
            ]
        logger.debug("flow record: {cmd}", cmd=_safe_command_for_log(command))
        process = self._popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_subprocess_env(),
        )
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            returncode = process.poll()
            if returncode is not None:
                _stdout, _stderr = process.communicate()
                raise RuntimeError(
                    f"openadapt-flow record stopped before authoring began (exit {returncode})"
                )
            if ready_path.exists():
                ready_path.unlink(missing_ok=True)
                return FlowRecordingSession(process, out_dir, stop_path, ready_path)
            time.sleep(0.05)
        process.terminate()
        process.communicate()
        raise RuntimeError("openadapt-flow record did not acknowledge authoring startup")

    def compile(
        self,
        recording_dir: Path,
        out_dir: Path,
        name: str | None = None,
        timeout: float | None = None,
    ) -> FlowResult:
        """Compile a recording directory into a bundle directory.

        ``openadapt-flow compile`` requires ``--name``; default it to the bundle
        directory name so a caller that only knows the output path still works.
        """
        args = [
            "compile",
            str(recording_dir),
            "--out",
            str(out_dir),
            "--name",
            name or out_dir.name,
        ]
        return self._run(args, out_dir=out_dir, timeout=timeout)

    def replay(
        self,
        bundle_dir: Path,
        out_dir: Path | None = None,
        url: str | None = None,
        timeout: float | None = None,
        *,
        config: Path | None = None,
        params_file: Path | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> FlowResult:
        """Replay a bundle; returns the run directory in ``out_dir`` if given."""
        args = ["replay", str(bundle_dir)]
        if out_dir:
            args += ["--run-dir", str(out_dir)]
        if config:
            args += ["--config", str(config)]
        if params_file:
            args += ["--params-file", str(params_file)]
        if url:
            args += ["--url", url]
        return self._run(
            args,
            out_dir=out_dir,
            timeout=timeout,
            env_overrides=env_overrides,
        )

    def run(
        self,
        bundle_dir: Path,
        config: Path,
        out_dir: Path | None = None,
        timeout: float | None = None,
        authorization_file: Path | None = None,
        params_file: Path | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> FlowResult:
        """Run a bundle under a deployment config.

        ``authorization_file`` forwards a cloud-minted GovernedRunAuthorization
        JSON to ``openadapt-flow run --authorization-file`` -- pass it only when
        :meth:`run_supports_authorization` is True (the flag is a PROPOSED flow
        follow-up in the 2026-07-17 runner-platform spec).
        """
        args = ["run", str(bundle_dir), "--config", str(config)]
        if out_dir:
            args += ["--run-dir", str(out_dir)]
        if authorization_file:
            args += ["--authorization-file", str(authorization_file)]
        if params_file:
            args += ["--params-file", str(params_file)]
        return self._run(
            args,
            out_dir=out_dir,
            timeout=timeout,
            env_overrides=env_overrides,
        )

    def qualify_run_case(
        self,
        bundle_dir: Path,
        config: Path,
        *,
        case_id: str,
        inputs_file: Path,
        campaign_id: str,
        run_id: str,
        out_dir: Path,
        url: str | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> FlowResult:
        """Run one qualification case through Flow-owned authorization.

        Desktop supplies only private canonical inputs and stable local ids.
        Flow builds and consumes the Standard authorization in the same process.
        """

        args = [
            "qualify",
            "run-case",
            str(bundle_dir),
            "--case-id",
            case_id,
            "--inputs",
            str(inputs_file),
            "--campaign-id",
            campaign_id,
            "--run-id",
            run_id,
            "--run-dir",
            str(out_dir),
            "--config",
            str(config),
        ]
        if url:
            args += ["--url", url]
        return self._run(args, out_dir=out_dir, env_overrides=env_overrides)

    def run_supports_authorization(self) -> bool:
        """Probe (once) whether the installed flow CLI accepts ``--authorization-file``."""
        if self._run_auth_support is None:
            try:
                result = self._run(["run", "--help"])
                self._run_auth_support = "--authorization-file" in (result.stdout or "")
            except Exception:
                self._run_auth_support = False
        return self._run_auth_support

    def supports_command(self, command: str) -> bool:
        """Best-effort probe for an optional Flow subcommand."""

        try:
            result = self._run([command, "--help"], timeout=15)
            if command == "push":
                self._push_json_support = result.ok and "--json" in (
                    result.stdout or ""
                )
            return result.ok
        except Exception:
            if command == "push":
                self._push_json_support = False
            return False

    def push(
        self,
        path: Path,
        *,
        kind: str,
        host: str,
        name: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
        env_overrides: dict[str, str] | None = None,
        json_output: bool = True,
    ) -> FlowResult:
        """Upload through the same pinned Flow runtime as every other verb."""

        if json_output and not self.push_supports_json():
            raise FlowNotAvailableError(
                "The pinned openadapt-flow runtime does not support the structured "
                "push-result contract. Update the exact Flow pin before hosted upload."
            )
        args = ["push", str(path), "--kind", kind, "--host", host]
        if json_output:
            args.append("--json")
        if name:
            # A Desktop task description can contain a record identity. Do not
            # put it in argv or logs. Cloud can suggest a safe display name.
            logger.warning("Desktop omitted a local workflow name from the Flow command")
        child_env = dict(env_overrides or {})
        if token:
            child_env[_INGEST_TOKEN_ENV] = token
        return self._run(args, timeout=timeout, env_overrides=child_env or None)

    def push_supports_json(self) -> bool:
        """Require Flow's machine-readable push contract before any upload."""

        if self._push_json_support is None:
            try:
                result = self._run(["push", "--help"], timeout=15)
                self._push_json_support = result.ok and "--json" in (result.stdout or "")
            except Exception:
                self._push_json_support = False
        return self._push_json_support

    def report_break(
        self,
        run_dir: Path,
        *,
        workflow_id: str,
        host: str,
        deployment_kind: str = "cloud",
        org_id: str | None = None,
        timeout: float | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> FlowResult:
        """Send Flow's closed-schema break summary.

        The bearer token belongs in ``env_overrides``. It must not appear in
        the child process argument list.
        """

        args = [
            "report-break",
            str(run_dir),
            "--workflow-id",
            workflow_id,
            "--host",
            host,
            "--deployment-kind",
            deployment_kind,
        ]
        if org_id:
            args += ["--org-id", org_id]
        return self._run(args, timeout=timeout, env_overrides=env_overrides)

    def teach(
        self,
        run_dir: Path,
        bundle_dir: Path,
        out_dir: Path,
        *,
        fix: Path,
        timeout: float | None = None,
    ) -> FlowResult:
        """Teach a fix for a halted run, producing a promoted bundle in ``out_dir``."""
        args = [
            "teach",
            str(run_dir),
            "--fix",
            str(fix),
            "--bundle",
            str(bundle_dir),
            "--out",
            str(out_dir),
        ]
        return self._run(args, out_dir=out_dir, timeout=timeout)

    # --- report / halt parsing (halt is read from report.json, not exit code) ---

    @staticmethod
    def read_report(run_dir: Path) -> dict:
        """Load ``report.json`` from a run directory (empty dict if absent)."""
        report_path = Path(run_dir) / "report.json"
        if not report_path.exists():
            return {}
        try:
            report = json.loads(report_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        return report if isinstance(report, dict) else {}

    @staticmethod
    def classify_outcome(returncode: int, report: dict) -> FlowOutcome:
        """Classify only explicit, internally consistent Flow outcomes.

        Current runtimes must carry a matching precise outcome envelope:
        only ``VERIFIED`` is a Desktop success. ``COMPLETED_UNVERIFIED``,
        ``FAILED``, and ``ROLLED_BACK`` remain explicit non-success states.
        Reports from earlier bundled runtimes retain the narrow legacy
        success/halt classification.
        """

        halt = report.get("halt")
        explicit_halt = report.get("terminal_outcome") == "halt" or (
            isinstance(halt, dict) and bool(halt)
        )
        precise = report.get("execution_outcome")
        if precise is not None:
            envelope = report.get("outcome_envelope")
            if precise not in PRECISE_FLOW_OUTCOMES or not isinstance(envelope, dict):
                return "unknown"
            if (
                envelope.get("version") != "openadapt.execution-outcome/v1"
                or envelope.get("outcome") != precise
                or not _valid_precise_envelope(report, envelope, precise)
            ):
                return "unknown"
            if precise == "VERIFIED":
                if returncode != 0 or report.get("success") is not True or explicit_halt:
                    return "unknown"
            return cast(FlowOutcome, precise)

        terminal = report.get("terminal_outcome")
        explicit_success = (
            returncode == 0
            and report.get("success") is True
            and terminal in (None, "success")
            and not explicit_halt
        )
        if explicit_success:
            return "success"
        if report.get("success") is not True and explicit_halt:
            return "halt"
        return "unknown"

    @classmethod
    def read_halt(cls, run_dir: Path) -> dict | None:
        """Return the ``halt`` block from a run's ``report.json``, or None.

        Recognizes both a nested ``{"halt": {...}}`` and a top-level
        ``HaltObservation``-shaped report with ``status == "halt"``.
        """
        report = cls.read_report(run_dir)
        halt = report.get("halt")
        if isinstance(halt, dict) and halt:
            return halt
        if report.get("terminal_outcome") == "halt":
            return {"outcome": "halt"}
        envelope = report.get("outcome_envelope")
        if (
            report.get("execution_outcome") == "HALTED"
            and isinstance(envelope, dict)
            and envelope.get("version") == "openadapt.execution-outcome/v1"
            and envelope.get("outcome") == "HALTED"
            and _valid_precise_envelope(report, envelope, "HALTED")
        ):
            return {"outcome": "HALTED"}
        if report.get("status") == "halt":
            return report
        return None
