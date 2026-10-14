"""Azure operations status tracker.

Writes real-time status to azure_ops_status.json for dashboard consumption.
Used by CLI commands (setup-waa, run-waa, vm monitor) to provide visibility
into long-running Azure operations.

Usage:
    from openadapt_evals.infrastructure.azure_ops_tracker import AzureOpsTracker

    tracker = AzureOpsTracker()
    tracker.start_operation("docker_build", total_steps=12)
    tracker.update(phase="pulling_base_image", step=1, log_lines=["Pulling from ..."])
    tracker.append_log("Step 1/12 : FROM dockurr/windows:latest")
    tracker.finish_operation()
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# VM pricing from vm_monitor.py
VM_HOURLY_RATES = {
    "Standard_D2_v3": 0.096,
    "Standard_D4_v3": 0.192,
    "Standard_D8_v3": 0.384,
    "Standard_D4s_v3": 0.192,
    "Standard_D8s_v3": 0.384,
    "Standard_D4ds_v5": 0.422,  # Updated pricing as per spec
    "Standard_D8ds_v5": 0.384,
    "Standard_D16ds_v5": 0.768,
    "Standard_D32ds_v5": 1.536,
}

# Typical operation durations in seconds (for ETA estimation)
TYPICAL_DURATIONS = {
    "docker_build": 600,  # ~10 minutes for waa-auto build
    "docker_pull": 300,  # ~5 minutes for large image pull
    "windows_boot": 900,  # ~15 minutes for first Windows boot
    "benchmark": 1800,  # ~30 minutes for 20 tasks
}

DEFAULT_OUTPUT_FILE = Path("benchmark_results/azure_ops_status.json")


@dataclass
class AzureOpsStatus:
    """Status of current Azure operation.

    Attributes:
        operation: Current operation type (idle, vm_create, docker_install,
            docker_build, windows_boot, benchmark, etc.)
        phase: Specific phase within the operation.
        step: Current step number.
        total_steps: Total number of steps in the operation.
        progress_pct: Progress percentage (0-100).
        log_tail: Last N lines of log output.
        started_at: ISO timestamp when operation started.
        elapsed_seconds: Seconds since operation started.
        eta_seconds: Estimated seconds remaining (None if unknown).
        cost_usd: Running cost in USD.
        hourly_rate_usd: Hourly VM rate in USD.
        vm_ip: VM IP address if available.
        vm_state: VM power state (running, starting, stopped, deallocated).
        vm_size: Azure VM size.
        vnc_url: VNC URL for accessing Windows desktop.
        error: Error message if operation failed.
        download_bytes: Bytes downloaded so far (for image pulls).
        download_total_bytes: Total bytes to download.
        build_id: Current Docker build run ID (to detect new builds).
    """

    operation: str = "idle"
    phase: str = ""
    step: int = 0
    total_steps: int = 0
    progress_pct: float = 0.0
    log_tail: list[str] = field(default_factory=list)
    started_at: str | None = None
    elapsed_seconds: float = 0.0
    eta_seconds: float | None = None
    cost_usd: float = 0.0
    hourly_rate_usd: float = 0.422  # Default for Standard_D4ds_v5
    vm_ip: str | None = None
    vm_state: str = "unknown"
    vm_size: str = "Standard_D4ds_v5"
    vnc_url: str | None = None
    error: str | None = None
    download_bytes: int = 0
    download_total_bytes: int = 0
    build_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


class AzureOpsTracker:
    """Tracks Azure operations and writes status to JSON file.

    The tracker maintains a status file that the dashboard can poll to
    display real-time progress of Azure operations.
    """

    MAX_LOG_LINES = 100

    def __init__(
        self,
        output_file: str | Path = DEFAULT_OUTPUT_FILE,
        vm_size: str = "Standard_D4ds_v5",
    ):
        """Initialize tracker.

        Args:
            output_file: Path to output JSON file.
            vm_size: Azure VM size for cost calculation.
        """
        self.output_file = Path(output_file)
        self.vm_size = vm_size
        self.hourly_rate = VM_HOURLY_RATES.get(vm_size, 0.422)
        self._status = AzureOpsStatus(
            vm_size=vm_size,
            hourly_rate_usd=self.hourly_rate,
        )
        self._start_time: datetime | None = None

    def start_operation(
        self,
        operation: str,
        total_steps: int = 0,
        phase: str = "",
        vm_ip: str | None = None,
        vm_state: str = "running",
        build_id: str | None = None,
        started_at: datetime | None = None,
    ) -> None:
        """Start tracking a new operation.

        Args:
            operation: Operation type (vm_create, docker_install, docker_build,
                windows_boot, benchmark, etc.)
            total_steps: Total number of steps in the operation.
            phase: Initial phase description.
            vm_ip: VM IP address if known.
            vm_state: VM power state.
            build_id: Unique identifier for this build (to detect new builds).
            started_at: When the operation actually started (uses now if not provided).
        """
        self._start_time = started_at or datetime.now()
        self._status = AzureOpsStatus(
            operation=operation,
            phase=phase,
            step=0,
            total_steps=total_steps,
            progress_pct=0.0,
            log_tail=[],  # Clear stale logs
            started_at=self._start_time.isoformat(),
            elapsed_seconds=0.0,
            eta_seconds=TYPICAL_DURATIONS.get(
                operation
            ),  # Use typical duration as initial ETA
            cost_usd=0.0,
            hourly_rate_usd=self.hourly_rate,
            vm_ip=vm_ip,
            vm_state=vm_state,
            vm_size=self.vm_size,
            vnc_url="http://localhost:8006" if vm_ip else None,
            error=None,
            download_bytes=0,
            download_total_bytes=0,
            build_id=build_id,
        )
        self._write_status()

    def update(
        self,
        phase: str | None = None,
        step: int | None = None,
        total_steps: int | None = None,
        log_lines: list[str] | None = None,
        vm_ip: str | None = None,
        vm_state: str | None = None,
        error: str | None = None,
        download_bytes: int | None = None,
        download_total_bytes: int | None = None,
        build_id: str | None = None,
    ) -> None:
        """Update operation status.

        Args:
            phase: Current phase description.
            step: Current step number.
            total_steps: Total steps (can be updated if discovered during operation).
            log_lines: New log lines to append.
            vm_ip: VM IP address.
            vm_state: VM power state.
            error: Error message if operation failed.
            download_bytes: Bytes downloaded so far.
            download_total_bytes: Total bytes to download.
            build_id: Build identifier (clears log if different from current).
        """
        # If build_id changed, this is a new build - clear stale logs
        if build_id is not None and build_id != self._status.build_id:
            self._status.build_id = build_id
            self._status.log_tail = []
            self._status.error = None
            self._start_time = datetime.now()
            self._status.started_at = self._start_time.isoformat()

        if phase is not None:
            self._status.phase = phase
        if step is not None:
            self._status.step = step
        if total_steps is not None:
            self._status.total_steps = total_steps
        if log_lines is not None:
            for line in log_lines:
                self.append_log(line)
        if vm_ip is not None:
            self._status.vm_ip = vm_ip
            self._status.vnc_url = "http://localhost:8006"
        if vm_state is not None:
            self._status.vm_state = vm_state
        if error is not None:
            self._status.error = error
        if download_bytes is not None:
            self._status.download_bytes = download_bytes
        if download_total_bytes is not None:
            self._status.download_total_bytes = download_total_bytes

        # Update derived fields
        self._update_progress()
        self._write_status()

    def append_log(self, line: str) -> None:
        """Append a log line (keeps last MAX_LOG_LINES).

        Args:
            line: Log line to append.
        """
        self._status.log_tail.append(line.rstrip())
        if len(self._status.log_tail) > self.MAX_LOG_LINES:
            self._status.log_tail = self._status.log_tail[-self.MAX_LOG_LINES :]
        self._update_progress()
        self._write_status()

    def parse_docker_build_line(self, line: str) -> dict[str, Any]:
        """Parse Docker build output for step progress and download info.

        Handles both patterns:
        - Old style: "Step X/Y : ..."
        - Buildx style: "#N [stage X/Y] ..." or "#N sha256:... XXXMB / YGB ..."

        Args:
            line: Docker build output line.

        Returns:
            Dict with parsed info: {step, total_steps, download_bytes, download_total_bytes, phase}
        """
        result: dict[str, Any] = {}

        # Old style: "Step X/Y : ..."
        step_match = re.search(r"Step\s+(\d+)/(\d+)", line)
        if step_match:
            result["step"] = int(step_match.group(1))
            result["total_steps"] = int(step_match.group(2))

        # Buildx style: "#N [stage X/Y] ..."
        buildx_stage = re.search(r"#\d+\s+\[.*?\s+(\d+)/(\d+)\]", line)
        if buildx_stage:
            result["step"] = int(buildx_stage.group(1))
            result["total_steps"] = int(buildx_stage.group(2))

        # Download progress: "sha256:... XXXMB / YGB ..." or "XXX.XXMB / YY.YYGB ..."
        download_match = re.search(
            r"(\d+(?:\.\d+)?)\s*(MB|GB|KB|B)\s*/\s*(\d+(?:\.\d+)?)\s*(MB|GB|KB|B)",
            line,
        )
        if download_match:
            size_multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}
            downloaded = float(download_match.group(1))
            downloaded_unit = download_match.group(2)
            total = float(download_match.group(3))
            total_unit = download_match.group(4)
            result["download_bytes"] = int(
                downloaded * size_multipliers[downloaded_unit]
            )
            result["download_total_bytes"] = int(total * size_multipliers[total_unit])

        # Extract phase from buildx output
        if line.startswith("#"):
            # #N DONE, #N CACHED, #N [stage]
            phase_match = re.match(r"#\d+\s+(.*)", line)
            if phase_match:
                phase_text = phase_match.group(1)[:80]
                # Clean up ANSI codes
                phase_text = re.sub(r"\x1b\[[0-9;]*m", "", phase_text)
                result["phase"] = phase_text.strip()

        # Apply updates if we found anything
        if "step" in result:
            self._status.step = result["step"]
        if "total_steps" in result:
            self._status.total_steps = result["total_steps"]
        if "download_bytes" in result:
            self._status.download_bytes = result["download_bytes"]
        if "download_total_bytes" in result:
            self._status.download_total_bytes = result["download_total_bytes"]
        if "phase" in result:
            self._status.phase = result["phase"]

        if result:
            self._update_progress()

        return result

    def is_error_line(self, line: str) -> bool:
        """Check if a line is an error message.

        Args:
            line: Log line to check.

        Returns:
            True if line contains an error.
        """
        error_patterns = [
            r"ERROR:",
            r"failed to build",
            r"failed to solve",
            r"error reading from server",
            r"rpc error",
        ]
        return any(re.search(p, line, re.IGNORECASE) for p in error_patterns)

    def finish_operation(self, success: bool = True, error: str | None = None) -> None:
        """Mark operation as complete.

        Args:
            success: Whether the operation completed successfully.
            error: Error message if operation failed.
        """
        if error:
            self._status.error = error
        self._status.operation = "complete" if success else "failed"
        self._status.progress_pct = 100.0 if success else self._status.progress_pct
        self._update_progress()
        self._write_status()

    def set_idle(self) -> None:
        """Reset tracker to idle state."""
        self._start_time = None
        self._status = AzureOpsStatus(
            vm_size=self.vm_size,
            hourly_rate_usd=self.hourly_rate,
        )
        self._write_status()

    def get_status(self) -> AzureOpsStatus:
        """Get current status (with updated elapsed time and cost)."""
        self._update_progress()
        return self._status

    def _update_progress(self) -> None:
        """Update derived fields (elapsed time, cost, progress percentage, ETA)."""
        # Update elapsed time
        if self._start_time:
            elapsed = datetime.now() - self._start_time
            self._status.elapsed_seconds = elapsed.total_seconds()

            # Update cost
            elapsed_hours = self._status.elapsed_seconds / 3600
            self._status.cost_usd = elapsed_hours * self.hourly_rate

        # Calculate progress from multiple sources
        progress_pct = 0.0
        eta_seconds = None

        # 1. Download progress (most accurate during image pulls)
        if self._status.download_total_bytes > 0:
            download_pct = (
                self._status.download_bytes / self._status.download_total_bytes
            ) * 100
            progress_pct = max(progress_pct, download_pct)

            # ETA from download speed
            if self._status.download_bytes > 0 and self._status.elapsed_seconds > 1:
                bytes_per_sec = (
                    self._status.download_bytes / self._status.elapsed_seconds
                )
                remaining_bytes = (
                    self._status.download_total_bytes - self._status.download_bytes
                )
                if bytes_per_sec > 0:
                    eta_seconds = remaining_bytes / bytes_per_sec

        # 2. Step-based progress
        if self._status.total_steps > 0:
            step_pct = (self._status.step / self._status.total_steps) * 100
            progress_pct = max(progress_pct, step_pct)

            # ETA from step rate (only if we have meaningful progress)
            if self._status.step > 0 and self._status.elapsed_seconds > 10:
                time_per_step = self._status.elapsed_seconds / self._status.step
                remaining_steps = self._status.total_steps - self._status.step
                step_eta = time_per_step * remaining_steps
                # Use step ETA if we don't have download ETA or if step progress > download
                if (
                    eta_seconds is None
                    or step_pct
                    > (
                        self._status.download_bytes
                        / max(self._status.download_total_bytes, 1)
                    )
                    * 100
                ):
                    eta_seconds = step_eta

        # 3. Fallback: Use typical duration if no progress info
        if eta_seconds is None and self._status.operation in TYPICAL_DURATIONS:
            typical = TYPICAL_DURATIONS[self._status.operation]
            remaining = max(0, typical - self._status.elapsed_seconds)
            eta_seconds = remaining
            # Estimate progress from elapsed vs typical
            if progress_pct == 0 and self._status.elapsed_seconds > 0:
                progress_pct = min(95, (self._status.elapsed_seconds / typical) * 100)

        self._status.progress_pct = min(100.0, progress_pct)
        self._status.eta_seconds = eta_seconds

    def _write_status(self) -> None:
        """Write current status to JSON file."""
        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.output_file, "w") as f:
            json.dump(self._status.to_dict(), f, indent=2)


# Global tracker instance for convenience
_tracker: AzureOpsTracker | None = None


def get_tracker(
    output_file: str | Path = DEFAULT_OUTPUT_FILE,
    vm_size: str = "Standard_D4ds_v5",
) -> AzureOpsTracker:
    """Get or create global tracker instance.

    Args:
        output_file: Path to output JSON file.
        vm_size: Azure VM size for cost calculation.

    Returns:
        AzureOpsTracker instance.
    """
    global _tracker
    if _tracker is None:
        _tracker = AzureOpsTracker(output_file=output_file, vm_size=vm_size)
    return _tracker


def read_status(
    status_file: str | Path = DEFAULT_OUTPUT_FILE,
) -> dict[str, Any]:
    """Read status from JSON file with fresh computed values.

    This function reads the persisted status and recomputes time-dependent
    fields (elapsed_seconds, cost_usd) based on the current time. This ensures
    the API always returns accurate values without relying on client-side
    computation.

    Args:
        status_file: Path to status JSON file.

    Returns:
        Status dictionary with fresh elapsed_seconds and cost_usd, or idle status
        if file doesn't exist.
    """
    status_path = Path(status_file)
    if status_path.exists():
        try:
            with open(status_path) as f:
                status = json.load(f)

            # Recompute time-dependent fields if operation is active
            if status.get("started_at") and status.get("operation") not in (
                "idle",
                "complete",
                "failed",
            ):
                started_at = datetime.fromisoformat(status["started_at"])
                elapsed = datetime.now() - started_at
                elapsed_seconds = max(0, elapsed.total_seconds())

                # Update elapsed time
                status["elapsed_seconds"] = elapsed_seconds

                # Update cost based on elapsed time
                hourly_rate = status.get("hourly_rate_usd", 0.422)
                status["cost_usd"] = (elapsed_seconds / 3600) * hourly_rate

                # Update ETA if we have progress info
                progress_pct = status.get("progress_pct", 0)
                if progress_pct > 0 and elapsed_seconds > 10:
                    # Estimate remaining time from progress rate
                    time_per_pct = elapsed_seconds / progress_pct
                    remaining_pct = 100 - progress_pct
                    status["eta_seconds"] = time_per_pct * remaining_pct
                elif status.get("operation") in TYPICAL_DURATIONS:
                    # Use typical duration minus elapsed
                    typical = TYPICAL_DURATIONS[status["operation"]]
                    status["eta_seconds"] = max(0, typical - elapsed_seconds)

            return status
        except (json.JSONDecodeError, IOError, ValueError):
            pass

    # Return default idle status
    return AzureOpsStatus().to_dict()
