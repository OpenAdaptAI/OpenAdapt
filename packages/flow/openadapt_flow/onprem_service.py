"""Unattended on-prem service: run the local queue, seal runs, report health.

``openadapt-flow service run`` is the long-running process a service manager
keeps alive (launchd on macOS, systemd on Linux, a Windows service wrapper).
Each cycle it:

1. runs ``deploy/on-prem/bin/run-queue.sh once`` with strict no-outbound mode
   set for the queue and every run it starts;
2. writes an offline Seal for any finished run that lacks one;
3. folds queued Seals into a local batch (nothing is sent);
4. writes ``service/heartbeat.json`` under the storage root.

It refuses to start without strict no-outbound mode unless the operator
passes ``--allow-outbound``, and the heartbeat records which it was.

``service health`` reads the heartbeat and the queue and exits non-zero when
the service is down, stale, stuck, or misconfigured. ``service restart`` asks
the service manager to restart it. ``service egress-audit`` samples the
operating system's socket table for the service and all its child processes
and records every remote peer it sees; see :func:`egress_audit`.

Nothing here calls a model, and nothing here changes how a run is graded.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from openadapt_flow import no_outbound

HEARTBEAT_SCHEMA = "openadapt.onprem-heartbeat/v1"
HEALTH_SCHEMA = "openadapt.onprem-health/v1"
EGRESS_AUDIT_SCHEMA = "openadapt.egress-audit/v1"
DEFAULT_LABEL = "ai.openadapt.flow-service"
BLOCKED_LOG = "outbound-blocked.jsonl"

#: (argv, env) -> return code. Injected by tests.
Runner = Callable[[Sequence[str], Mapping[str, str]], int]
#: argv -> stdout text. Injected by tests.
Reader = Callable[[Sequence[str]], str]


def _utc(now: Optional[datetime] = None) -> str:
    return (
        (now or datetime.now(timezone.utc))
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _parse_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _default_runner(argv: Sequence[str], env: Mapping[str, str]) -> int:
    return subprocess.run(list(argv), env=dict(env), check=False).returncode


def _default_reader(argv: Sequence[str]) -> str:
    proc = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    return proc.stdout


def default_queue_script() -> Path:
    """``deploy/on-prem/bin/run-queue.sh`` in a source checkout."""
    return (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "on-prem"
        / "bin"
        / "run-queue.sh"
    )


@dataclass
class ServiceConfig:
    root: Path
    queue_script: Path = field(default_factory=default_queue_script)
    poll_seconds: float = 10.0
    allow_outbound: bool = False
    max_cycles: Optional[int] = None


def paths(root: Path) -> dict[str, Path]:
    root = Path(root)
    return {
        "heartbeat": root / "service" / "heartbeat.json",
        "inbox": root / "jobs" / "inbox",
        "processing": root / "jobs" / "processing",
        "failed": root / "jobs" / "failed",
        "runs": root / "runs",
        "audit": root / "audit",
        "blocked_log": root / "audit" / BLOCKED_LOG,
        "seal_home": root / "seal",
    }


def child_env(cfg: ServiceConfig, base: Mapping[str, str]) -> dict[str, str]:
    """Environment for the queue and every run it starts."""
    p = paths(cfg.root)
    env = dict(base)
    env["OPENADAPT_ONPREM_ROOT"] = str(cfg.root)
    env["OPENADAPT_SEAL_HOME"] = str(p["seal_home"])
    env.setdefault("OPENADAPT_FLOW_SCRUB", "on")
    if not cfg.allow_outbound:
        env["OPENADAPT_NO_OUTBOUND"] = "1"
        env["OPENADAPT_NO_OUTBOUND_LOG"] = str(p["blocked_log"])
    return env


def seal_unsealed_runs(runs_dir: Path, seal_home: Path) -> "tuple[int, list[str]]":
    """Seal every finished run that has a report and no Seal yet."""
    from openadapt_flow.offline_seal import SEAL_FILE, LocalSealError, seal_run

    sealed, errors = 0, []
    if not runs_dir.is_dir():
        return 0, []
    for run in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        if (run / "report.json").is_file() and not (run / SEAL_FILE).exists():
            try:
                seal_run(run, home=seal_home)
                sealed += 1
            except LocalSealError as exc:
                errors.append(f"{run.name}: {exc.code}")
    return sealed, errors


def run_service(
    cfg: ServiceConfig,
    *,
    env: Optional[Mapping[str, str]] = None,
    runner: Runner = _default_runner,
    stop: Optional[threading.Event] = None,
    enforce: Callable[[Mapping[str, str]], None] = no_outbound.enforce,
    install_signals: bool = True,
) -> int:
    """Run the service loop until stopped or ``max_cycles`` is reached."""
    from openadapt_flow.seal_batch import build_batch

    base = dict(os.environ if env is None else env)
    explicitly_off = bool(str(base.get(no_outbound.ENV_MODE, "")).strip()) and (
        not no_outbound.enabled(base)
    )
    if explicitly_off and not cfg.allow_outbound:
        print(
            "service: refusing to start: OPENADAPT_NO_OUTBOUND is set to "
            f"{base.get(no_outbound.ENV_MODE)!r}; pass --allow-outbound to run "
            "without strict no-outbound mode",
            file=sys.stderr,
        )
        return 2
    if not cfg.queue_script.is_file():
        print(f"service: queue script not found: {cfg.queue_script}", file=sys.stderr)
        return 2
    p = paths(cfg.root)
    for key in ("inbox", "processing", "failed", "runs", "audit"):
        p[key].mkdir(parents=True, exist_ok=True)
    cenv = child_env(cfg, base)
    if not cfg.allow_outbound:
        enforce(cenv)  # this process too, not just its children
    seal_home = Path(cenv["OPENADAPT_SEAL_HOME"])
    stop = stop or threading.Event()
    if install_signals:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
    started = _utc()
    cycles = 0
    beat: dict[str, Any] = {
        "schema": HEARTBEAT_SCHEMA,
        "pid": os.getpid(),
        "started_at": started,
        "poll_seconds": cfg.poll_seconds,
        "no_outbound": not cfg.allow_outbound,
        "state": "running",
    }
    while not stop.is_set():
        errors: list[str] = []
        try:
            rc = runner(["bash", str(cfg.queue_script), "once"], cenv)
        except OSError as exc:
            rc, errors = 127, [f"queue: {type(exc).__name__}"]
        sealed, seal_errors = seal_unsealed_runs(p["runs"], seal_home)
        errors.extend(seal_errors)
        try:
            batch = build_batch(seal_home)
        except Exception as exc:  # a batch problem is reported, never fatal
            batch, errors = None, errors + [f"batch: {type(exc).__name__}"]
        cycles += 1
        beat.update(
            {
                "last_beat": _utc(),
                "cycles": cycles,
                "last_cycle": {
                    "queue_rc": rc,
                    "sealed": sealed,
                    "batch": batch.name if batch else None,
                    "errors": errors,
                },
            }
        )
        _write_json(p["heartbeat"], beat)
        if cfg.max_cycles is not None and cycles >= cfg.max_cycles:
            break
        stop.wait(cfg.poll_seconds)
    beat.update({"state": "stopped", "last_beat": _utc()})
    _write_json(p["heartbeat"], beat)
    return 0


# --------------------------------------------------------------------- health


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # pass | warn | fail
    detail: str = ""


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) would terminate the process on Windows.
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _win_pid_alive(pid: int) -> bool:  # pragma: no cover - Windows only
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def health(
    root: Path,
    *,
    now: Optional[datetime] = None,
    max_age_s: Optional[float] = None,
    stuck_after_s: float = 3600.0,
) -> dict[str, Any]:
    """Health of the service at ``root``: every check, and an overall verdict."""
    now = now or datetime.now(timezone.utc)
    p = paths(Path(root))
    checks: list[Check] = []
    beat: dict[str, Any] = {}
    try:
        beat = json.loads(p["heartbeat"].read_text(encoding="utf-8"))
        checks.append(Check("heartbeat", "pass"))
    except FileNotFoundError:
        checks.append(
            Check("heartbeat", "fail", "no heartbeat: the service has not run")
        )
    except (OSError, ValueError):
        checks.append(Check("heartbeat", "fail", "heartbeat unreadable"))
    if beat:
        poll = float(beat.get("poll_seconds") or 10.0)
        limit = max_age_s if max_age_s is not None else max(60.0, 3 * poll)
        try:
            age = (now - _parse_utc(str(beat.get("last_beat")))).total_seconds()
        except ValueError:
            age = float("inf")
        if beat.get("state") == "stopped":
            checks.append(Check("running", "fail", "the service stopped"))
        elif not _pid_alive(beat.get("pid")):
            checks.append(
                Check("running", "fail", f"pid {beat.get('pid')} is not running")
            )
        else:
            checks.append(Check("running", "pass", f"pid {beat.get('pid')}"))
        checks.append(
            Check(
                "fresh",
                "pass" if age <= limit else "fail",
                f"last heartbeat {age:.0f}s ago (limit {limit:.0f}s)",
            )
        )
        checks.append(
            Check(
                "no_outbound",
                "pass" if beat.get("no_outbound") else "warn",
                "strict no-outbound mode is on"
                if beat.get("no_outbound")
                else "running with --allow-outbound",
            )
        )
        last = beat.get("last_cycle") or {}
        if last.get("errors"):
            checks.append(Check("last_cycle", "warn", "; ".join(last["errors"])[:500]))
        elif last.get("queue_rc") not in (0, None):
            checks.append(
                Check("last_cycle", "warn", f"queue exit {last.get('queue_rc')}")
            )
        else:
            checks.append(Check("last_cycle", "pass"))
    stuck = []
    if p["processing"].is_dir():
        for job in p["processing"].glob("*.job"):
            age = now.timestamp() - job.stat().st_mtime
            if age > stuck_after_s:
                stuck.append(job.name)
    checks.append(
        Check(
            "stuck_jobs",
            "fail" if stuck else "pass",
            ", ".join(sorted(stuck))[:500] if stuck else "",
        )
    )
    failed = len(list(p["failed"].glob("*.job"))) if p["failed"].is_dir() else 0
    checks.append(
        Check("failed_jobs", "warn" if failed else "pass", f"{failed} in failed/")
    )
    seal_home = p["seal_home"]
    key = seal_home / "keys" / "ed25519.pem"
    if key.exists():
        mode = key.stat().st_mode & 0o777
        checks.append(
            Check(
                "seal_key",
                "pass" if mode & 0o077 == 0 else "fail",
                f"mode {mode:o}",
            )
        )
    else:
        checks.append(
            Check("seal_key", "warn", "no Seal key yet (created on first Seal)")
        )
    unsealed = 0
    if p["runs"].is_dir():
        for run in p["runs"].iterdir():
            if (run / "report.json").is_file() and not (run / "seal.json").exists():
                unsealed += 1
    checks.append(Check("unsealed_runs", "warn" if unsealed else "pass", str(unsealed)))
    blocked = 0
    if p["blocked_log"].is_file():
        blocked = sum(
            1 for line in p["blocked_log"].read_text().splitlines() if line.strip()
        )
    checks.append(
        Check(
            "blocked_outbound",
            "warn" if blocked else "pass",
            f"{blocked} refused connection attempts logged" if blocked else "none",
        )
    )
    healthy = all(c.status != "fail" for c in checks)
    return {
        "schema": HEALTH_SCHEMA,
        "checked_at": _utc(now),
        "healthy": healthy,
        "checks": [c.__dict__ for c in checks],
    }


def restart_command(
    label: str = DEFAULT_LABEL, platform: Optional[str] = None
) -> list[str]:
    """The service manager command that restarts the service."""
    platform = platform or sys.platform
    if platform == "darwin":
        return ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"]
    if platform.startswith("linux"):
        return ["systemctl", "restart", label]
    if platform == "win32":
        return [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Restart-Service -Name {label}",
        ]
    raise ValueError(f"no service manager known for {platform}")


def restart(
    label: str = DEFAULT_LABEL,
    *,
    runner: Runner = _default_runner,
    platform: Optional[str] = None,
) -> int:
    return runner(restart_command(label, platform), dict(os.environ))


# --------------------------------------------------------------- egress audit

_LSOF_PEER = re.compile(r"->(\[[^\]]+\]|[^:\s]+):(\d+|\*)")
_LSOF_LOCAL = re.compile(r"^(\[[^\]]+\]|[^:\s]+|\*):(\d+|\*)")


def process_tree(root_pid: int, ps_output: str) -> set[int]:
    """``root_pid`` and all its descendants, from ``ps -A -o pid=,ppid=``."""
    children: dict[int, list[int]] = {}
    for line in ps_output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    seen, todo = {root_pid}, [root_pid]
    while todo:
        for child in children.get(todo.pop(), []):
            if child not in seen:
                seen.add(child)
                todo.append(child)
    return seen


def parse_lsof(output: str) -> list[dict[str, Any]]:
    """Parse ``lsof -nP -i`` output into one entry per socket."""
    entries = []
    for line in output.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 9 or not cols[1].isdigit():
            continue
        name_cols = cols[8:]
        name = name_cols[0]
        state = name_cols[1].strip("()") if len(name_cols) > 1 else ""
        proto = cols[7]
        peer = _LSOF_PEER.search(name)
        local = _LSOF_LOCAL.match(name)
        entry: dict[str, Any] = {
            "command": cols[0],
            "pid": int(cols[1]),
            "proto": proto,
            "state": state,
            "local_host": local.group(1).strip("[]") if local else None,
            "remote_host": peer.group(1).strip("[]") if peer else None,
            "remote_port": peer.group(2) if peer else None,
        }
        entries.append(entry)
    return entries


def classify(
    entries: Iterable[Mapping[str, Any]], allow: Sequence[Any] = ()
) -> dict[str, list]:
    """Split sockets into local peers, outbound peers and exposed listeners."""
    out: dict[str, list] = {"local": [], "outbound": [], "listening_exposed": []}
    for e in entries:
        remote = e.get("remote_host")
        if remote:
            if no_outbound.host_is_local(remote, allow):
                out["local"].append(dict(e))
            else:
                out["outbound"].append(dict(e))
        elif e.get("state") == "LISTEN" or e.get("proto") == "UDP":
            local = e.get("local_host")
            if local in ("*", None) or not no_outbound.host_is_local(local, allow):
                out["listening_exposed"].append(dict(e))
    return out


def egress_audit(
    root: Path,
    *,
    pid: Optional[int] = None,
    samples: int = 5,
    interval_s: float = 2.0,
    reader: Reader = _default_reader,
    sleep: Callable[[float], None] = time.sleep,
    env: Optional[Mapping[str, str]] = None,
    write: bool = True,
) -> dict[str, Any]:
    """Sample the socket table of the service's whole process tree.

    The verdict is ``clean`` only if every sample succeeded and no socket of
    the service or any descendant had a peer off this machine (outside the
    no-outbound allow list). Sampling shows what was open at each sample; it
    cannot see a connection that opened and closed between samples. That is
    why the in-process hook refuses those, and why its log is counted here.
    The written report is sealed with the local key.
    """
    env = os.environ if env is None else env
    p = paths(Path(root))
    allow = no_outbound.parse_allow(env.get(no_outbound.ENV_ALLOW))
    if pid is None:
        try:
            pid = int(json.loads(p["heartbeat"].read_text(encoding="utf-8"))["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            pid = None
    started = _utc()
    report: dict[str, Any] = {
        "schema": EGRESS_AUDIT_SCHEMA,
        "started_at": started,
        "root_pid": pid,
        "samples_requested": samples,
        "interval_s": interval_s,
    }
    if pid is None or not _pid_alive(pid):
        report.update({"verdict": "unavailable", "detail": "service is not running"})
        return _finish_audit(report, p, write)
    if shutil.which("lsof") is None and reader is _default_reader:
        report.update({"verdict": "unavailable", "detail": "lsof not found"})
        return _finish_audit(report, p, write)
    pids_seen: set[int] = set()
    seen: dict[str, dict[str, Any]] = {}
    outbound: dict[str, dict[str, Any]] = {}
    exposed: dict[str, dict[str, Any]] = {}
    ok_samples = 0
    for i in range(max(1, samples)):
        tree = process_tree(pid, reader(["ps", "-A", "-o", "pid=,ppid="]))
        pids_seen |= tree
        text = reader(
            ["lsof", "-nP", "-i", "-a", "-p", ",".join(str(x) for x in sorted(tree))]
        )
        ok_samples += 1
        groups = classify(parse_lsof(text), allow)
        for e in groups["local"]:
            seen.setdefault(f"{e['remote_host']}:{e['remote_port']}", e)
        for e in groups["outbound"]:
            outbound.setdefault(f"{e['pid']}->{e['remote_host']}:{e['remote_port']}", e)
        for e in groups["listening_exposed"]:
            exposed.setdefault(f"{e['pid']}:{e['proto']}:{e['local_host']}", e)
        if i + 1 < samples:
            sleep(interval_s)
    blocked = 0
    if p["blocked_log"].is_file():
        blocked = sum(
            1 for line in p["blocked_log"].read_text().splitlines() if line.strip()
        )
    report.update(
        {
            "ended_at": _utc(),
            "samples_taken": ok_samples,
            "pids_seen": sorted(pids_seen),
            "local_peers": sorted(seen),
            "outbound_peers": [
                {
                    k: e[k]
                    for k in ("command", "pid", "proto", "remote_host", "remote_port")
                }
                for e in outbound.values()
            ],
            "exposed_listeners": [
                {k: e[k] for k in ("command", "pid", "proto", "local_host")}
                for e in exposed.values()
            ],
            "blocked_attempts_logged": blocked,
            "verdict": "outbound_seen" if outbound else "clean",
        }
    )
    return _finish_audit(report, p, write)


def _finish_audit(
    report: dict[str, Any], p: Mapping[str, Path], write: bool
) -> dict[str, Any]:
    if not write:
        return report
    stamp = report["started_at"].replace(":", "").replace("-", "")
    out = p["audit"] / f"egress-audit-{stamp}.json"
    _write_json(out, report)
    report_path = str(out)
    try:
        from openadapt_flow.offline_seal import sign_file

        seal = sign_file(out, subject_kind="file", run_id=out.stem, home=p["seal_home"])
        _write_json(out.with_name(out.name + ".seal.json"), seal.wire())
        report = dict(report, report_path=report_path, sealed=True)
    except Exception:
        report = dict(report, report_path=report_path, sealed=False)
    return report
