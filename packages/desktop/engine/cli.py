"""CLI entry point for the OpenAdapt Desktop engine.

Provides a command-line interface for recording, scrubbing, reviewing,
and uploading captures without requiring the Tauri shell.

Usage:
    openadapt-desktop record [--quality standard] [--task "description"]
    openadapt-desktop list [--limit 10] [--status captured]
    openadapt-desktop info CAPTURE_ID
    openadapt-desktop scrub CAPTURE_ID [--level basic]
    openadapt-desktop review
    openadapt-desktop approve CAPTURE_ID
    openadapt-desktop dismiss CAPTURE_ID
    openadapt-desktop upload CAPTURE_ID --backend s3
    openadapt-desktop backends
    openadapt-desktop storage
    openadapt-desktop health
    openadapt-desktop cleanup
    openadapt-desktop config
    openadapt-desktop doctor
"""

from __future__ import annotations

import argparse
import os
import sys
import types
from pathlib import Path

from loguru import logger

from engine.config import EngineConfig


def _format_bytes(n: int) -> str:
    """Format bytes as human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} PB"


def _init_engine(config: EngineConfig) -> types.SimpleNamespace:
    """Initialize engine subsystems."""
    from engine.audit import AuditLogger
    from engine.db import IndexDB
    from engine.storage_manager import StorageManager

    config.data_dir.mkdir(parents=True, exist_ok=True)

    audit = AuditLogger(config.audit_log_path, enabled=config.network_audit_log)
    db = IndexDB(config.data_dir / "index.db")
    db.initialize()

    storage = StorageManager(config)
    storage.initialize()
    # Share the DB instance so storage uses the same connection
    storage._db = db

    return types.SimpleNamespace(config=config, audit=audit, db=db, storage=storage)


def _create_backends(config: EngineConfig) -> list:
    """Create backend instances based on config.

    The hosted ingest backend (POST /api/ingest, bearer token) is always
    registered -- it is the default cloud-lane sink. S3 is optional BYOC
    customer-owned storage.
    """
    from engine.backends.hosted_ingest import HostedIngestBackend

    backends = [HostedIngestBackend(host=config.hosted_host)]

    if config.s3_bucket:
        from engine.backends.s3 import S3Backend

        backends.append(S3Backend(
            bucket=config.s3_bucket,
            region=config.s3_region,
            access_key_id=config.s3_access_key_id,
            secret_access_key=config.s3_secret_access_key,
            endpoint=config.s3_endpoint,
        ))
    return backends


def cmd_record(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """Start recording."""
    from engine.controller import RecordingController

    controller = RecordingController(
        captures_dir=engine.config.data_dir / "captures",
        quality=args.quality,
        storage_manager=engine.storage,
    )

    # Recover incomplete sessions
    recovered = controller.recover()
    if recovered:
        print(f"Recovered {len(recovered)} incomplete session(s)")

    task = getattr(args, "task", None) or ""
    capture_id = controller.start(quality=args.quality, task_description=task)
    print(f"Recording started: {capture_id}")
    print("Press Ctrl+C to stop recording...")

    engine.audit.log("recording_started", capture_id=capture_id)

    try:
        import signal

        signal.pause()
    except (KeyboardInterrupt, AttributeError):
        pass

    metadata = controller.stop()
    print("Recording stopped.")
    print(f"  ID:       {metadata['id']}")
    print(f"  Duration: {metadata['duration']:.1f}s")
    print(f"  Events:   {metadata['event_count']}")
    print(f"  Size:     {_format_bytes(metadata['size_bytes'])}")


def cmd_list(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """List captures."""
    status = getattr(args, "status", None)
    captures = engine.storage.get_captures(limit=args.limit, review_status=status)
    if not captures:
        print("No captures found.")
        return

    print(f"{'ID':<12} {'Started':<22} {'Duration':<10} {'Status':<12} {'Size':<10}")
    print("-" * 66)
    for c in captures:
        dur = f"{c.get('duration_secs', 0):.0f}s" if c.get("duration_secs") else "..."
        size = _format_bytes(c.get("size_bytes", 0))
        started = c.get("started_at", "")[:19]
        print(
            f"{c['capture_id']:<12} {started:<22} {dur:<10} "
            f"{c['review_status']:<12} {size:<10}"
        )


def cmd_info(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """Show capture details."""
    cap = engine.db.get_capture(args.capture_id)
    if not cap:
        print(f"Capture not found: {args.capture_id}")
        sys.exit(1)
    for key, val in cap.items():
        print(f"  {key}: {val}")


def cmd_scrub(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """Scrub PII from a capture."""
    from engine.review import ReviewStatus, transition_status
    from engine.scrubber import Scrubber, ScrubLevel

    cap = engine.db.get_capture(args.capture_id)
    if not cap:
        print(f"Capture not found: {args.capture_id}")
        sys.exit(1)

    scrubber = Scrubber(level=ScrubLevel(args.level))
    scrubbed_path = scrubber.scrub_capture(Path(cap["capture_path"]))

    transition_status(
        args.capture_id,
        ReviewStatus.CAPTURED,
        ReviewStatus.SCRUBBED,
        db=engine.db,
        audit=engine.audit,
    )
    engine.db.update_capture(args.capture_id, scrubbed_path=str(scrubbed_path))

    print(f"Scrubbed ({args.level}): {scrubbed_path}")


def cmd_review(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """List pending reviews."""
    from engine.review import get_pending_reviews

    pending = get_pending_reviews(engine.db)
    if not pending:
        print("No captures pending review.")
        return

    print(f"{'ID':<12} {'Status':<12} {'Started':<22}")
    print("-" * 46)
    for c in pending:
        started = c.get("started_at", "")[:19]
        print(f"{c['capture_id']:<12} {c['review_status']:<12} {started:<22}")


def cmd_approve(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """Approve a scrubbed capture for upload."""
    from engine.review import ReviewStatus, transition_status

    transition_status(
        args.capture_id,
        ReviewStatus.SCRUBBED,
        ReviewStatus.REVIEWED,
        db=engine.db,
        audit=engine.audit,
    )
    print(f"Approved: {args.capture_id}")


def cmd_dismiss(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """Dismiss scrubbing, accept PII risks."""
    from engine.review import ReviewStatus, transition_status

    transition_status(
        args.capture_id,
        ReviewStatus.CAPTURED,
        ReviewStatus.DISMISSED,
        db=engine.db,
        audit=engine.audit,
    )
    print(f"Dismissed (raw data cleared for egress): {args.capture_id}")


def cmd_upload(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """Upload a capture to a backend."""
    from engine.upload_manager import UploadManager

    backends = _create_backends(engine.config)
    manager = UploadManager(engine.config, backends, engine.db, engine.audit)

    job_id = manager.enqueue(args.capture_id, args.backend)
    print(f"Upload queued: job {job_id[:8]}")

    results = manager.process_queue()
    for r in results:
        if r["success"]:
            print(f"Upload complete: {r['remote_url']}")
        else:
            print(f"Upload failed: {r['error']}")


def cmd_login(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """Authenticate to the hosted control plane (browser PKCE or token paste)."""
    from engine import auth

    host = getattr(args, "host", None) or engine.config.hosted_host
    prefer = getattr(args, "provider", None)
    try:
        cred = auth.login(host=host, prefer=prefer)
    except Exception as exc:
        print(f"Login failed: {exc}")
        sys.exit(1)
    print(f"Logged in to {cred['host']} (org={cred.get('org_id') or 'unknown'}).")
    engine.audit.log("hosted_login", host=cred["host"], kind=cred["kind"])


def cmd_push(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """Zip a recording/bundle directory and push it to /api/ingest."""
    from engine import hosted

    host = getattr(args, "host", None) or engine.config.hosted_host
    path = Path(args.path) if getattr(args, "path", None) else None
    recordings_dir = engine.config.data_dir / "recordings"
    try:
        result = hosted.push(
            path,
            kind=args.kind,
            name=getattr(args, "name", None),
            host=host,
            token=getattr(args, "token", None),
            recordings_dir=recordings_dir,
        )
    except FileNotFoundError as exc:
        print(f"Nothing to push: {exc}")
        sys.exit(1)

    if result["success"]:
        print(f"Pushed. Workflow: {result['workflow_id']}")
        if result["dashboard_url"]:
            print(f"  {result['dashboard_url']}")
        engine.audit.log("hosted_push", workflow_id=result["workflow_id"], kind=args.kind)
    else:
        print(f"Push failed: {result['error']}")
        sys.exit(1)


def cmd_compile(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """Compile a flow recording directory into a bundle (delegates to openadapt-flow)."""
    from engine.flow_bridge import FlowBridge, FlowNotAvailableError

    default_out = engine.config.data_dir / "bundles" / Path(args.recording).name
    out = Path(args.out) if getattr(args, "out", None) else default_out
    try:
        result = FlowBridge().compile(Path(args.recording), out)
    except FlowNotAvailableError as exc:
        print(str(exc))
        sys.exit(1)
    print("Compiled." if result.ok else f"Compile failed:\n{result.stderr}")
    if not result.ok:
        sys.exit(result.returncode or 1)
    print(f"  Bundle: {out}")


def cmd_replay(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """Replay a bundle locally (delegates to openadapt-flow)."""
    from engine.flow_bridge import FlowBridge, FlowNotAvailableError

    out = Path(args.out) if getattr(args, "out", None) else None
    try:
        result = FlowBridge().replay(Path(args.bundle), out_dir=out, url=getattr(args, "url", None))
    except FlowNotAvailableError as exc:
        print(str(exc))
        sys.exit(1)
    print(result.stdout or ("Replay complete." if result.ok else result.stderr))
    if not result.ok:
        sys.exit(result.returncode or 1)


def cmd_run(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """Run a bundle under a deployment config (delegates to openadapt-flow)."""
    from engine.flow_bridge import FlowBridge, FlowNotAvailableError

    out = Path(args.out) if getattr(args, "out", None) else None
    try:
        result = FlowBridge().run(Path(args.bundle), Path(args.config), out_dir=out)
    except FlowNotAvailableError as exc:
        print(str(exc))
        sys.exit(1)
    print(result.stdout or ("Run complete." if result.ok else result.stderr))
    if not result.ok:
        sys.exit(result.returncode or 1)


def cmd_report_break(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """Emit a PHI-free break descriptor for a halted run to /api/runs/ingest-report."""
    from engine import hosted

    host = getattr(args, "host", None) or engine.config.hosted_host
    result = hosted.report_break(
        Path(args.run_dir),
        workflow_id=getattr(args, "workflow_id", None),
        host=host,
        deployment_kind=engine.config.deployment_lane,
        token=getattr(args, "token", None),
    )
    if result.get("ok"):
        print(f"Reported break: halt {result.get('halt_id')}")
        if result.get("teach_url"):
            print(f"  Teach: {host}{result['teach_url']}")
    elif result.get("local_teach"):
        print("Break kept local (PHI boundary): teach the fix locally.")
    else:
        print(f"Report failed: {result.get('error')}")
        sys.exit(1)


def cmd_backends(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """List available backends."""
    backends = _create_backends(engine.config)
    if not backends:
        print("No backends configured.")
        return
    for b in backends:
        print(f"  {b.name}: credentials={'valid' if b.verify_credentials() else 'invalid'}")


def cmd_storage(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """Show storage usage."""
    usage = engine.storage.get_storage_usage()
    print("Storage usage:")
    print(f"  Total:    {_format_bytes(usage['used_bytes'])} / {_format_bytes(usage['max_bytes'])}")
    print(f"  Hot:      {_format_bytes(usage['hot_bytes'])}")
    print(f"  Warm:     {_format_bytes(usage['warm_bytes'])}")
    print(f"  Cold:     {_format_bytes(usage['cold_bytes'])}")
    print(f"  Captures: {usage['capture_count']}")


def cmd_health(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """Show engine health."""
    from engine.monitor import HealthMonitor

    monitor = HealthMonitor(engine.config)
    health = monitor.get_health_status()
    mem = health["memory"]
    disk = health["disk"]
    print("Health status:")
    print(f"  Memory:  {mem['rss_mb']} MB (threshold: {mem['threshold_mb']} MB)")
    print(f"  Disk:    {disk['usage_percent']}% used")
    print(f"  Uptime:  {health['uptime_secs']}s")


def cmd_cleanup(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """Run storage cleanup."""
    actions = engine.storage.run_cleanup()
    print("Cleanup complete:")
    print(f"  Archived: {actions['archived']}")
    print(f"  Deleted:  {actions['deleted']}")
    print(f"  Freed:    {_format_bytes(actions['bytes_freed'])}")


def cmd_config(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """Show current configuration."""
    print(engine.config.model_dump_json(indent=2))


def cmd_doctor(args: argparse.Namespace, engine: types.SimpleNamespace) -> None:
    """Check system dependencies and configuration."""
    from engine import __version__

    checks: list[tuple[str, bool, str]] = []

    # Engine version
    checks.append(("Engine version", True, f"v{__version__}"))

    # Python version
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 11)
    checks.append(("Python", py_ok, py_ver if py_ok else f"{py_ver} (need >=3.11)"))

    # Data directory
    data_ok = engine.config.data_dir.exists() and os.access(engine.config.data_dir, os.W_OK)
    checks.append(("Data directory", data_ok, str(engine.config.data_dir)))

    # Database
    try:
        engine.db.conn.execute("SELECT 1").fetchone()
        checks.append(("Database (SQLite)", True, "connected"))
    except Exception as e:
        checks.append(("Database (SQLite)", False, str(e)))

    # openadapt-capture
    try:
        import openadapt_capture
        ver = getattr(openadapt_capture, "__version__", "installed")
        checks.append(("openadapt-capture", True, ver))
    except ImportError:
        checks.append(("openadapt-capture", False, "not installed (recording disabled)"))

    # openadapt-privacy
    try:
        import openadapt_privacy
        ver = getattr(openadapt_privacy, "__version__", "installed")
        checks.append(("openadapt-privacy", True, ver))
    except ImportError:
        checks.append(("openadapt-privacy", False, "not installed (advanced scrubbing disabled)"))

    # psutil
    try:
        import psutil
        checks.append(("psutil", True, psutil.__version__))
    except ImportError:
        checks.append(("psutil", False, "not installed (health monitoring disabled)"))

    # httpx (hosted ingest / auth)
    try:
        import httpx
        checks.append(("httpx (hosted ingest)", True, httpx.__version__))
    except ImportError:
        checks.append(("httpx (hosted ingest)", False, "not installed"))

    # keyring (credential store)
    try:
        import keyring
        checks.append(("keyring (credential store)", True,
                       getattr(keyring, "__version__", "installed")))
    except ImportError:
        checks.append(("keyring (credential store)", False, "not installed"))

    # openadapt-flow (the loop engine)
    from engine.flow_bridge import flow_available
    checks.append((
        "openadapt-flow (loop engine)",
        flow_available(),
        "on PATH" if flow_available() else "not found (pip install openadapt-flow)",
    ))

    # boto3 (optional BYOC storage)
    try:
        import boto3
        checks.append(("boto3 (S3 backend)", True, boto3.__version__))
    except ImportError:
        checks.append(("boto3 (S3 backend)", False,
                       "not installed (pip install openadapt-desktop[enterprise])"))

    # Hosted control plane
    checks.append(("Hosted host", True, engine.config.hosted_host))
    checks.append(("Deployment lane", True, engine.config.deployment_lane))

    # Hosted credential
    from engine.auth.store import auth_header
    logged_in = "Authorization" in auth_header()
    checks.append(("Hosted credential", logged_in,
                   "present" if logged_in else "not logged in (run 'openadapt login')"))

    # S3 credentials (if configured)
    if engine.config.s3_bucket:
        has_creds = bool(engine.config.s3_access_key_id and engine.config.s3_secret_access_key)
        detail = f"bucket={engine.config.s3_bucket}" if has_creds else "bucket set but keys missing"
        checks.append(("S3 credentials", has_creds, detail))

    # Print results
    print("OpenAdapt Doctor")
    print("=" * 60)
    ok_count = sum(1 for _, ok, _ in checks if ok)
    for name, ok, detail in checks:
        marker = "OK" if ok else "!!"
        print(f"  [{marker}] {name}: {detail}")

    print("=" * 60)
    total = len(checks)
    print(f"{ok_count}/{total} checks passed")

    if ok_count < total:
        print("\nRun 'pip install openadapt-desktop[full]' to install all optional dependencies.")


_COMMANDS = {
    "record": cmd_record,
    "list": cmd_list,
    "info": cmd_info,
    "scrub": cmd_scrub,
    "review": cmd_review,
    "approve": cmd_approve,
    "dismiss": cmd_dismiss,
    "upload": cmd_upload,
    "login": cmd_login,
    "push": cmd_push,
    "compile": cmd_compile,
    "replay": cmd_replay,
    "run": cmd_run,
    "report-break": cmd_report_break,
    "backends": cmd_backends,
    "storage": cmd_storage,
    "health": cmd_health,
    "cleanup": cmd_cleanup,
    "config": cmd_config,
    "doctor": cmd_doctor,
}


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="openadapt-desktop",
        description="Experimental OpenAdapt Desktop capture/review CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # record
    p = subparsers.add_parser("record", help="Start recording")
    p.add_argument("--quality", default="standard", choices=["low", "standard", "high", "lossless"])
    p.add_argument("--task", default=None, help="Task description")

    # list
    p = subparsers.add_parser("list", help="List captures")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--status", default=None)

    # info
    p = subparsers.add_parser("info", help="Show capture details")
    p.add_argument("capture_id")

    # scrub
    p = subparsers.add_parser("scrub", help="Scrub PII from capture")
    p.add_argument("capture_id")
    p.add_argument("--level", default="basic", choices=["basic", "standard", "enhanced"])

    # review
    subparsers.add_parser("review", help="List pending reviews")

    # approve
    p = subparsers.add_parser("approve", help="Approve capture for upload")
    p.add_argument("capture_id")

    # dismiss
    p = subparsers.add_parser("dismiss", help="Dismiss scrubbing")
    p.add_argument("capture_id")

    # upload
    p = subparsers.add_parser("upload", help="Upload capture to a storage backend")
    p.add_argument("capture_id")
    p.add_argument("--backend", required=True, choices=["hosted_ingest", "s3"])

    # login
    p = subparsers.add_parser("login", help="Authenticate to the hosted control plane")
    p.add_argument("--host", default=None, help="Hosted base URL")
    p.add_argument("--provider", default=None, choices=["paste", "browser_pkce"],
                   help="Force an auth provider")

    # push
    p = subparsers.add_parser("push", help="Push a recording/bundle to /api/ingest")
    p.add_argument("path", nargs="?", default=None, help="Recording/bundle dir (default: latest)")
    p.add_argument("--kind", default="recording", choices=["recording", "bundle"])
    p.add_argument("--name", default=None, help="Workflow name")
    p.add_argument("--host", default=None, help="Hosted base URL")
    p.add_argument("--token", default=None, help="Ingest token (else keychain/env)")

    # compile
    p = subparsers.add_parser("compile", help="Compile a recording into a flow bundle")
    p.add_argument("recording", help="Recording directory")
    p.add_argument("--out", default=None, help="Output bundle directory")

    # replay
    p = subparsers.add_parser("replay", help="Replay a flow bundle locally")
    p.add_argument("bundle", help="Bundle directory")
    p.add_argument("--out", default=None, help="Run output directory")
    p.add_argument("--url", default=None, help="Target URL override")

    # run
    p = subparsers.add_parser("run", help="Run a flow bundle under a deployment config")
    p.add_argument("bundle", help="Bundle directory")
    p.add_argument("--config", required=True, help="Deployment config path")
    p.add_argument("--out", default=None, help="Run output directory")

    # report-break
    p = subparsers.add_parser("report-break", help="Report a halted run to the cloud (PHI-free)")
    p.add_argument("run_dir", help="Run directory containing report.json")
    p.add_argument("--workflow-id", dest="workflow_id", default=None, help="Hosted workflow id")
    p.add_argument("--host", default=None, help="Hosted base URL")
    p.add_argument("--token", default=None, help="Ingest token (else keychain/env)")

    # backends
    subparsers.add_parser("backends", help="List available backends")

    # storage
    subparsers.add_parser("storage", help="Show storage usage")

    # health
    subparsers.add_parser("health", help="Show engine health")

    # cleanup
    subparsers.add_parser("cleanup", help="Run storage cleanup")

    # config
    subparsers.add_parser("config", help="Show configuration")

    # doctor
    subparsers.add_parser("doctor", help="Check dependencies and configuration")

    args = parser.parse_args(argv)

    config = EngineConfig()

    logger.remove()
    logger.add(sys.stderr, level=config.log_level)

    engine = _init_engine(config)

    try:
        _COMMANDS[args.command](args, engine)
    finally:
        engine.db.close()
