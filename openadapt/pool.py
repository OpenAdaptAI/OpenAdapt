"""Container worker pool launcher wrapping Linux ``xfreerdp3 + Xvfb``.

Supports ``openadapt pool up --workers N --backend xfreerdp``, ``status``, and
``down`` via Docker or OrbStack, including explicit container ``--platform``
selection on macOS Apple Silicon (``linux/amd64`` or native ``linux/arm64``),
healthcheck metadata, dry-run command-plan generation, and injectable subprocess
execution for headless unit testing.
"""

from __future__ import annotations

import json
import platform as _platform
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

POOL_STATE_SCHEMA = "openadapt.pool-state/v1"
DEFAULT_POOL_STATE_DIR = Path.home() / ".openadapt" / "pool"
DEFAULT_POOL_NAME = "openadapt-rdp"
DEFAULT_IMAGE = "ghcr.io/openadaptai/openadapt-xfreerdp-worker:latest"
SUPPORTED_BACKENDS = ("xfreerdp",)
SUPPORTED_RUNTIMES = ("auto", "docker", "orbstack")
SUPPORTED_PLATFORMS = ("auto", "linux/amd64", "linux/arm64")


class PoolError(ValueError):
    """Raised when pool configuration or container orchestration fails."""


@dataclass(frozen=True)
class CommandResult:
    """Normalized subprocess execution result."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[Sequence[str]], CommandResult]


@dataclass(frozen=True)
class WorkerSpec:
    """Planned or running worker container metadata."""

    worker_id: str
    container_name: str
    display: str
    backend: str
    image: str
    platform: str
    healthcheck: dict[str, Any]
    command: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "container_name": self.container_name,
            "display": self.display,
            "backend": self.backend,
            "image": self.image,
            "platform": self.platform,
            "healthcheck": dict(self.healthcheck),
            "command": list(self.command),
        }


@dataclass(frozen=True)
class PoolPlan:
    """Execution plan and result for ``openadapt pool up``."""

    pool_name: str
    runtime: str
    backend: str
    platform: str
    image: str
    dry_run: bool
    workers: tuple[WorkerSpec, ...] = field(default_factory=tuple)
    started_containers: tuple[str, ...] = field(default_factory=tuple)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": POOL_STATE_SCHEMA,
            "pool_name": self.pool_name,
            "runtime": self.runtime,
            "backend": self.backend,
            "platform": self.platform,
            "image": self.image,
            "dry_run": self.dry_run,
            "worker_count": len(self.workers),
            "workers": [w.to_dict() for w in self.workers],
            "started_containers": list(self.started_containers),
            "created_at": self.created_at,
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_command_runner(argv: Sequence[str]) -> CommandResult:
    proc = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=False,
    )
    return CommandResult(
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def detect_container_runtime(
    requested: str = "auto",
    *,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> str:
    """Resolve the container CLI executable (``docker`` or OrbStack-backed ``docker``)."""
    if requested not in SUPPORTED_RUNTIMES:
        raise PoolError(
            f"Unsupported runtime {requested!r}; expected one of {SUPPORTED_RUNTIMES}."
        )
    if requested == "docker":
        return "docker"
    if requested == "orbstack":
        # OrbStack exposes the Docker-compatible CLI via `docker` (with orbstack context)
        # alongside `orbctl`. Prefer `docker` if present, otherwise `orbctl`.
        return "docker" if which("docker") else "orbctl"
    if which("docker"):
        return "docker"
    if which("orbctl"):
        return "orbctl"
    return "docker"


def resolve_container_platform(
    requested: str = "auto",
    *,
    host_system: Optional[str] = None,
    host_machine: Optional[str] = None,
) -> str:
    """Resolve target container platform, handling macOS Apple Silicon explicitly."""
    if requested not in SUPPORTED_PLATFORMS:
        raise PoolError(
            f"Unsupported platform {requested!r}; expected one of {SUPPORTED_PLATFORMS}."
        )
    if requested != "auto":
        return requested

    system = (host_system or _platform.system()).lower()
    machine = (host_machine or _platform.machine()).lower()
    if system == "darwin" and machine in ("arm64", "aarch64"):
        # Default to linux/amd64 on Apple Silicon for xfreerdp3 image compatibility;
        # callers can opt into native arm64 with --platform linux/arm64.
        return "linux/amd64"
    if machine in ("arm64", "aarch64"):
        return "linux/arm64"
    return "linux/amd64"


def _build_healthcheck(display: str) -> dict[str, Any]:
    cmd = (
        f"test -S /tmp/.X11-unix/X{display.lstrip(':')} "
        "&& command -v xfreerdp3 >/dev/null 2>&1"
    )
    return {
        "test": ["CMD-SHELL", cmd],
        "interval": "15s",
        "timeout": "5s",
        "retries": 3,
        "start_period": "5s",
    }


def build_worker_spec(
    *,
    index: int,
    pool_name: str,
    runtime: str,
    backend: str,
    image: str,
    container_platform: str,
    display_base: int = 99,
    screen_resolution: str = "1920x1080x24",
    rdp_host: Optional[str] = None,
    extra_env: Optional[dict[str, str]] = None,
) -> WorkerSpec:
    """Build the container launch command and healthcheck metadata for one worker."""
    worker_id = f"worker-{index}"
    container_name = f"{pool_name}-{worker_id}"
    display = f":{display_base + index}"
    healthcheck = _build_healthcheck(display)

    cmd: list[str] = [
        runtime,
        "run",
        "-d",
        "--name",
        container_name,
        "--platform",
        container_platform,
        "--label",
        f"openadapt.pool={pool_name}",
        "--label",
        f"openadapt.worker={worker_id}",
        "--label",
        f"openadapt.backend={backend}",
        "-e",
        f"DISPLAY={display}",
        "-e",
        f"XVFB_RESOLUTION={screen_resolution}",
        "-e",
        f"OPENADAPT_BACKEND={backend}",
        "--health-cmd",
        healthcheck["test"][1],
        "--health-interval",
        healthcheck["interval"],
        "--health-timeout",
        healthcheck["timeout"],
        "--health-retries",
        str(healthcheck["retries"]),
    ]
    if rdp_host:
        cmd.extend(["-e", f"RDP_HOST={rdp_host}"])
    if extra_env:
        for key, val in sorted(extra_env.items()):
            cmd.extend(["-e", f"{key}={val}"])

    entrypoint_script = (
        f"Xvfb {display} -screen 0 {screen_resolution} -ac +extension GLX +render -noreset & "
        f"XVFB_PID=$!; "
        f"until test -S /tmp/.X11-unix/X{display.lstrip(':')}; do sleep 0.2; done; "
        "command -v xfreerdp3 >/dev/null 2>&1 || { echo 'xfreerdp3 missing' >&2; exit 127; }; "
        "wait $XVFB_PID"
    )
    cmd.extend([image, "/bin/sh", "-c", entrypoint_script])

    return WorkerSpec(
        worker_id=worker_id,
        container_name=container_name,
        display=display,
        backend=backend,
        image=image,
        platform=container_platform,
        healthcheck=healthcheck,
        command=tuple(cmd),
    )


def _state_file(state_dir: Path, pool_name: str) -> Path:
    return state_dir / f"{pool_name}.json"


def pool_up(
    *,
    workers: int,
    backend: str = "xfreerdp",
    pool_name: str = DEFAULT_POOL_NAME,
    runtime: str = "auto",
    container_platform: str = "auto",
    image: str = DEFAULT_IMAGE,
    display_base: int = 99,
    screen_resolution: str = "1920x1080x24",
    rdp_host: Optional[str] = None,
    dry_run: bool = False,
    state_dir: Optional[Path] = None,
    runner: CommandRunner = default_command_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
    host_system: Optional[str] = None,
    host_machine: Optional[str] = None,
) -> PoolPlan:
    """Launch (or plan with ``dry_run=True``) N containerized xfreerdp3 + Xvfb workers."""
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise PoolError(f"--workers must be a positive integer, got {workers!r}.")
    if backend not in SUPPORTED_BACKENDS:
        raise PoolError(
            f"Unsupported backend {backend!r}; expected one of {SUPPORTED_BACKENDS}."
        )

    resolved_runtime = detect_container_runtime(runtime, which=which)
    resolved_platform = resolve_container_platform(
        container_platform,
        host_system=host_system,
        host_machine=host_machine,
    )

    specs = tuple(
        build_worker_spec(
            index=i,
            pool_name=pool_name,
            runtime=resolved_runtime,
            backend=backend,
            image=image,
            container_platform=resolved_platform,
            display_base=display_base,
            screen_resolution=screen_resolution,
            rdp_host=rdp_host,
        )
        for i in range(1, workers + 1)
    )

    started: list[str] = []
    if not dry_run:
        for spec in specs:
            res = runner(spec.command)
            if res.returncode != 0:
                raise PoolError(
                    f"Failed to start {spec.container_name}: {(res.stderr or res.stdout).strip()}"
                )
            started.append(res.stdout.strip() or spec.container_name)

    plan = PoolPlan(
        pool_name=pool_name,
        runtime=resolved_runtime,
        backend=backend,
        platform=resolved_platform,
        image=image,
        dry_run=dry_run,
        workers=specs,
        started_containers=tuple(started),
        created_at=_utc_now_iso(),
    )

    if not dry_run:
        target_state_dir = state_dir or DEFAULT_POOL_STATE_DIR
        target_state_dir.mkdir(parents=True, exist_ok=True)
        _state_file(target_state_dir, pool_name).write_text(
            json.dumps(plan.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return plan


def pool_status(
    *,
    pool_name: str = DEFAULT_POOL_NAME,
    runtime: str = "auto",
    state_dir: Optional[Path] = None,
    runner: CommandRunner = default_command_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> dict[str, Any]:
    """Query status and health of containers in ``pool_name``."""
    resolved_runtime = detect_container_runtime(runtime, which=which)
    cmd = [
        resolved_runtime,
        "ps",
        "-a",
        "--filter",
        f"label=openadapt.pool={pool_name}",
        "--format",
        "{{.Names}}\t{{.State}}\t{{.Status}}\t{{.ID}}",
    ]
    res = runner(cmd)
    if res.returncode != 0:
        raise PoolError(
            f"Failed to query pool status: {(res.stderr or res.stdout).strip()}"
        )

    containers: list[dict[str, Any]] = []
    for line in res.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split("\t")
        name = parts[0] if len(parts) > 0 else ""
        state = parts[1] if len(parts) > 1 else "unknown"
        status_text = parts[2] if len(parts) > 2 else ""
        cid = parts[3] if len(parts) > 3 else ""
        health = "unknown"
        lowered = status_text.lower()
        if "(healthy)" in lowered:
            health = "healthy"
        elif "(unhealthy)" in lowered:
            health = "unhealthy"
        elif "(starting)" in lowered or "health: starting" in lowered:
            health = "starting"
        containers.append(
            {
                "container_name": name,
                "container_id": cid,
                "state": state,
                "status": status_text,
                "health": health,
            }
        )

    target_state_dir = state_dir or DEFAULT_POOL_STATE_DIR
    saved_state: Optional[dict[str, Any]] = None
    sf = _state_file(target_state_dir, pool_name)
    if sf.is_file():
        try:
            saved_state = json.loads(sf.read_text(encoding="utf-8"))
        except ValueError:
            saved_state = None

    return {
        "pool_name": pool_name,
        "runtime": resolved_runtime,
        "running": sum(1 for c in containers if c["state"] == "running"),
        "total": len(containers),
        "containers": containers,
        "saved_state": saved_state,
    }


def pool_down(
    *,
    pool_name: str = DEFAULT_POOL_NAME,
    runtime: str = "auto",
    dry_run: bool = False,
    state_dir: Optional[Path] = None,
    runner: CommandRunner = default_command_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> dict[str, Any]:
    """Stop and remove all containers belonging to ``pool_name``."""
    resolved_runtime = detect_container_runtime(runtime, which=which)
    target_state_dir = state_dir or DEFAULT_POOL_STATE_DIR
    sf = _state_file(target_state_dir, pool_name)

    container_names: list[str] = []
    if sf.is_file():
        try:
            doc = json.loads(sf.read_text(encoding="utf-8"))
            for worker in doc.get("workers") or []:
                if isinstance(worker, dict) and worker.get("container_name"):
                    container_names.append(str(worker["container_name"]))
        except ValueError:
            pass

    if not container_names:
        ps_cmd = [
            resolved_runtime,
            "ps",
            "-a",
            "--filter",
            f"label=openadapt.pool={pool_name}",
            "--format",
            "{{.Names}}",
        ]
        res = runner(ps_cmd)
        if res.returncode == 0:
            container_names = [
                line.strip() for line in res.stdout.splitlines() if line.strip()
            ]

    commands: list[list[str]] = [
        [resolved_runtime, "rm", "-f", name] for name in container_names
    ]
    removed: list[str] = []
    if not dry_run:
        for name, cmd in zip(container_names, commands):
            res = runner(cmd)
            if res.returncode != 0:
                raise PoolError(
                    f"Failed to remove container {name}: {(res.stderr or res.stdout).strip()}"
                )
            removed.append(name)
        if sf.is_file():
            sf.unlink()

    return {
        "pool_name": pool_name,
        "runtime": resolved_runtime,
        "dry_run": dry_run,
        "commands": commands,
        "removed": removed,
    }
