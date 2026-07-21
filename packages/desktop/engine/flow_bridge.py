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

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from loguru import logger

FLOW_BIN = "openadapt-flow"
EMBEDDED_FLOW_MODE = "__openadapt_flow__"


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


class FlowBridge:
    """Invokes the ``openadapt-flow`` CLI for the local loop steps.

    Args:
        flow_bin: The flow executable name/path.
        runner: Callable with ``subprocess.run`` semantics (injected in tests).
    """

    def __init__(self, flow_bin: str = FLOW_BIN, runner=subprocess.run) -> None:
        self.flow_bin = flow_bin
        self._runner = runner
        self._run_auth_support: bool | None = None

    # --- low-level ---

    def _run(
        self, args: list[str], out_dir: Path | None = None, timeout: float | None = None
    ) -> FlowResult:
        prefix = _flow_command(self.flow_bin)
        if prefix is None:
            raise FlowNotAvailableError(
                f"'{self.flow_bin}' not found on PATH; install openadapt-flow."
            )
        cmd = [*prefix, *args]
        logger.debug("flow: {cmd}", cmd=" ".join(cmd))
        proc = self._runner(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_subprocess_env(),
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

    def replay(self, bundle_dir: Path, out_dir: Path | None = None, url: str | None = None,
               timeout: float | None = None) -> FlowResult:
        """Replay a bundle; returns the run directory in ``out_dir`` if given."""
        args = ["replay", str(bundle_dir)]
        if out_dir:
            args += ["--run-dir", str(out_dir)]
        if url:
            args += ["--url", url]
        return self._run(args, out_dir=out_dir, timeout=timeout)

    def run(self, bundle_dir: Path, config: Path, out_dir: Path | None = None,
            timeout: float | None = None,
            authorization_file: Path | None = None) -> FlowResult:
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
        return self._run(args, out_dir=out_dir, timeout=timeout)

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
            return self._run([command, "--help"], timeout=15).ok
        except Exception:
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
    ) -> FlowResult:
        """Upload through the same pinned Flow runtime as every other verb."""

        args = ["push", str(path), "--kind", kind, "--host", host]
        if name:
            args += ["--name", name]
        if token:
            args += ["--token", token]
        return self._run(args, timeout=timeout)

    def teach(self, run_dir: Path, bundle_dir: Path, out_dir: Path, fix: Path | None = None,
              timeout: float | None = None) -> FlowResult:
        """Teach a fix for a halted run, producing a promoted bundle in ``out_dir``."""
        args = ["teach", str(run_dir), "--bundle", str(bundle_dir), "--out", str(out_dir)]
        if fix:
            args += ["--fix", str(fix)]
        return self._run(args, out_dir=out_dir, timeout=timeout)

    # --- report / halt parsing (halt is read from report.json, not exit code) ---

    @staticmethod
    def read_report(run_dir: Path) -> dict:
        """Load ``report.json`` from a run directory (empty dict if absent)."""
        report_path = Path(run_dir) / "report.json"
        if not report_path.exists():
            return {}
        try:
            return json.loads(report_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

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
        if report.get("status") == "halt":
            return report
        return None
