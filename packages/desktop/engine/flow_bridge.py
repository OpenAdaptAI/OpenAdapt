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
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

FLOW_BIN = "openadapt-flow"


class FlowNotAvailableError(RuntimeError):
    """Raised when the ``openadapt-flow`` CLI cannot be located."""


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


def flow_available(flow_bin: str = FLOW_BIN) -> bool:
    """Whether the ``openadapt-flow`` CLI is on PATH."""
    return shutil.which(flow_bin) is not None


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
        if shutil.which(self.flow_bin) is None and self.flow_bin == FLOW_BIN:
            raise FlowNotAvailableError(
                f"'{self.flow_bin}' not found on PATH; install openadapt-flow."
            )
        cmd = [self.flow_bin, *args]
        logger.debug("flow: {cmd}", cmd=" ".join(cmd))
        proc = self._runner(cmd, capture_output=True, text=True, timeout=timeout)
        return FlowResult(
            ok=proc.returncode == 0,
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            out_dir=out_dir,
        )

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
        self, recording_dir: Path, out_dir: Path, timeout: float | None = None
    ) -> FlowResult:
        """Compile a recording directory into a bundle directory."""
        args = ["compile", str(recording_dir), "--out", str(out_dir)]
        return self._run(args, out_dir=out_dir, timeout=timeout)

    def replay(self, bundle_dir: Path, out_dir: Path | None = None, url: str | None = None,
               timeout: float | None = None) -> FlowResult:
        """Replay a bundle; returns the run directory in ``out_dir`` if given."""
        args = ["replay", str(bundle_dir)]
        if out_dir:
            args += ["--out", str(out_dir)]
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
            args += ["--out", str(out_dir)]
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
