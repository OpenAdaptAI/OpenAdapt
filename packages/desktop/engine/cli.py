"""CLI entry point for the OpenAdapt Desktop engine.

Provides a command-line interface for recording, scrubbing, reviewing,
and uploading captures without requiring the Tauri shell.

Usage:
    openadapt record [--quality standard] [--task "description"]
    openadapt list [--limit 10] [--status captured]
    openadapt info CAPTURE_ID
    openadapt scrub CAPTURE_ID [--level basic]
    openadapt review
    openadapt approve CAPTURE_ID
    openadapt dismiss CAPTURE_ID
    openadapt upload CAPTURE_ID --backend s3
    openadapt backends
    openadapt storage
    openadapt health
    openadapt cleanup
    openadapt config
    openadapt doctor
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
    """Create backend instances based on config."""
    backends = []
    if config.s3_bucket:
        from engine.backends.s3 import S3Backend

        backends.append(S3Backend(
            bucket=config.s3_bucket,
            region=config.s3_region,
            access_key_id=config.s3_access_key_id,
            secret_access_key=config.s3_secret_access_key,
            endpoint=config.s3_endpoint,
        ))
    if config.hf_token:
        from engine.backends.huggingface import HuggingFaceBackend

        backends.append(HuggingFaceBackend(repo=config.hf_repo, token=config.hf_token))

    from engine.backends.wormhole import WormholeBackend

    backends.append(WormholeBackend())
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

    # boto3 (optional)
    try:
        import boto3
        checks.append(("boto3 (S3 backend)", True, boto3.__version__))
    except ImportError:
        checks.append(("boto3 (S3 backend)", False,
                       "not installed (pip install openadapt-desktop[enterprise])"))

    # huggingface_hub (optional)
    try:
        import huggingface_hub
        checks.append(("huggingface_hub (HF backend)", True, huggingface_hub.__version__))
    except ImportError:
        checks.append(("huggingface_hub (HF backend)", False,
                       "not installed (pip install openadapt-desktop[community])"))

    # magic-wormhole
    import shutil
    wormhole_path = shutil.which("wormhole")
    checks.append((
        "magic-wormhole (P2P backend)",
        wormhole_path is not None,
        wormhole_path or "not found (pip install magic-wormhole)",
    ))

    # Storage mode
    checks.append(("Storage mode", True, engine.config.storage_mode))

    # S3 credentials (if configured)
    if engine.config.s3_bucket:
        has_creds = bool(engine.config.s3_access_key_id and engine.config.s3_secret_access_key)
        detail = f"bucket={engine.config.s3_bucket}" if has_creds else "bucket set but keys missing"
        checks.append(("S3 credentials", has_creds, detail))

    # HF token (if configured)
    if engine.config.hf_token:
        checks.append(("HuggingFace token", True, f"repo={engine.config.hf_repo}"))

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
    "backends": cmd_backends,
    "storage": cmd_storage,
    "health": cmd_health,
    "cleanup": cmd_cleanup,
    "config": cmd_config,
    "doctor": cmd_doctor,
}


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(prog="openadapt", description="OpenAdapt Desktop Engine")
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
    p = subparsers.add_parser("upload", help="Upload capture")
    p.add_argument("capture_id")
    p.add_argument("--backend", required=True, choices=["s3", "huggingface", "wormhole"])

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
