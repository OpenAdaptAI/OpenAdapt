"""Command-line interface for openadapt-viewer.

Usage:
    openadapt-viewer benchmark --data DIR [--output FILE] [--standalone]
    openadapt-viewer capture [--capture-id ID] [--goal GOAL] [--output FILE]
    openadapt-viewer demo [--output FILE]
    openadapt-viewer catalog scan [--capture-dir DIR] [--segmentation-dir DIR]
    openadapt-viewer catalog list [--json]
    openadapt-viewer catalog stats
"""

import argparse
import json as json_module
import sqlite3
import sys
import webbrowser
from pathlib import Path

from openadapt_viewer.viewers.benchmark import generate_benchmark_html


def main():
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(
        description="Generate standalone HTML viewers for OpenAdapt ML results",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Generate benchmark viewer from results directory
    openadapt-viewer benchmark --data benchmark_results/run_001/

    # Generate with embedded resources (standalone)
    openadapt-viewer benchmark --data results/ --standalone

    # Generate demo viewer with sample data
    openadapt-viewer demo --output demo.html --open
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Benchmark command
    benchmark_parser = subparsers.add_parser(
        "benchmark", help="Generate a benchmark viewer"
    )
    benchmark_parser.add_argument(
        "--data",
        "-d",
        type=Path,
        help=(
            "Path to a capture/benchmark directory "
            "(defaults to $OPENADAPT_CAPTURE_RECORDING)"
        ),
    )
    benchmark_parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("benchmark_viewer.html"),
        help="Output HTML file path (default: benchmark_viewer.html)",
    )
    benchmark_parser.add_argument(
        "--standalone",
        "-s",
        action="store_true",
        help="Embed all resources for offline viewing",
    )
    benchmark_parser.add_argument(
        "--open",
        action="store_true",
        help="Open the generated file in browser",
    )

    # Segmentation command
    seg_parser = subparsers.add_parser(
        "segmentation", help="Generate segmentation viewer with catalog integration"
    )
    seg_parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("segmentation_viewer_catalog.html"),
        help="Output HTML file path (default: segmentation_viewer_catalog.html)",
    )
    seg_parser.add_argument(
        "--auto-load",
        type=str,
        help="Recording ID to auto-load on page load",
    )
    seg_parser.add_argument(
        "--open",
        action="store_true",
        help="Open the generated file in browser",
    )

    # Capture command
    capture_parser = subparsers.add_parser(
        "capture", help="Generate capture viewer with playback controls"
    )
    capture_parser.add_argument(
        "--capture-id",
        type=str,
        default="capture",
        help="Capture recording ID (default: capture)",
    )
    capture_parser.add_argument(
        "--goal",
        type=str,
        help="Goal or description of the capture task",
    )
    capture_parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("capture_viewer.html"),
        help="Output HTML file path (default: capture_viewer.html)",
    )
    capture_parser.add_argument(
        "--open",
        action="store_true",
        help="Open the generated file in browser",
    )

    # Demo command
    demo_parser = subparsers.add_parser(
        "demo", help="Generate a demo viewer with sample data"
    )
    demo_parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("demo_viewer.html"),
        help="Output HTML file path (default: demo_viewer.html)",
    )
    demo_parser.add_argument(
        "--tasks",
        "-n",
        type=int,
        default=10,
        help="Number of sample tasks (default: 10)",
    )
    demo_parser.add_argument(
        "--open",
        action="store_true",
        help="Open the generated file in browser",
    )

    # Screenshots command
    screenshots_parser = subparsers.add_parser(
        "screenshots", help="Generate screenshots of viewers for documentation"
    )
    screenshots_subparsers = screenshots_parser.add_subparsers(
        dest="screenshots_command", help="Screenshot generation commands"
    )

    # screenshots segmentation
    seg_screenshots_parser = screenshots_subparsers.add_parser(
        "segmentation", help="Generate segmentation viewer screenshots"
    )
    seg_screenshots_parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Output directory for screenshots (default: screenshots/segmentation/)",
    )
    seg_screenshots_parser.add_argument(
        "--viewer",
        type=Path,
        help="Path to segmentation viewer HTML (default: segmentation_viewer.html)",
    )
    seg_screenshots_parser.add_argument(
        "--test-data",
        type=Path,
        help="Path to test episodes JSON (default: test_episodes.json)",
    )
    seg_screenshots_parser.add_argument(
        "--skip-responsive",
        action="store_true",
        help="Skip responsive (tablet/mobile) screenshots",
    )
    seg_screenshots_parser.add_argument(
        "--save-metadata",
        action="store_true",
        help="Save metadata JSON alongside screenshots",
    )
    seg_screenshots_parser.add_argument(
        "--viewport",
        choices=["desktop", "tablet", "mobile"],
        help="Generate only specific viewport screenshots",
    )

    # Catalog command
    catalog_parser = subparsers.add_parser(
        "catalog", help="Manage recording catalog"
    )
    catalog_subparsers = catalog_parser.add_subparsers(
        dest="catalog_command", help="Catalog operations"
    )

    # catalog scan
    scan_parser = catalog_subparsers.add_parser(
        "scan", help="Scan directories for recordings and segmentation results"
    )
    scan_parser.add_argument(
        "--capture-dir",
        type=Path,
        action="append",
        dest="capture_dirs",
        help="Directory to scan for recordings (can specify multiple times)",
    )
    scan_parser.add_argument(
        "--segmentation-dir",
        type=Path,
        action="append",
        dest="segmentation_dirs",
        help="Directory to scan for segmentation results (can specify multiple times)",
    )

    # catalog list
    list_parser = catalog_subparsers.add_parser(
        "list", help="List all recordings in catalog"
    )
    list_parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )
    list_parser.add_argument(
        "--with-segmentations",
        action="store_true",
        help="Include segmentation results for each recording",
    )

    # catalog stats
    catalog_subparsers.add_parser("stats", help="Show catalog statistics")

    # catalog clean
    catalog_subparsers.add_parser("clean", help="Remove entries for missing files")

    # catalog register
    register_parser = catalog_subparsers.add_parser(
        "register", help="Manually register a recording"
    )
    register_parser.add_argument(
        "path",
        type=Path,
        help="Path to recording directory",
    )
    register_parser.add_argument(
        "--name",
        help="Display name (defaults to directory name)",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "benchmark":
        run_benchmark_command(args)
    elif args.command == "segmentation":
        run_segmentation_command(args)
    elif args.command == "capture":
        run_capture_command(args)
    elif args.command == "demo":
        run_demo_command(args)
    elif args.command == "screenshots":
        run_screenshots_command(args)
    elif args.command == "catalog":
        run_catalog_command(args)


def run_benchmark_command(args):
    """Handle the benchmark command.

    POLICY: Defaults to real recording data, from $OPENADAPT_CAPTURE_RECORDING.
    Only uses sample data if explicitly disabled in code.
    """
    if args.data and not args.data.exists():
        print(f"Error: Data directory not found: {args.data}", file=sys.stderr)
        sys.exit(1)

    if args.data is None:
        print("Generating benchmark viewer with REAL recording data from $OPENADAPT_CAPTURE_RECORDING...")
    else:
        print(f"Generating benchmark viewer from: {args.data}")
    try:
        output_path = generate_benchmark_html(
            data_path=args.data,
            output_path=args.output,
            standalone=args.standalone,
            use_real_data=True,  # ALWAYS use real data by default
        )
    except FileNotFoundError as exc:
        # Recordings are local data. Report the missing one plainly instead of
        # dumping a traceback at anyone who has not configured one yet.
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Generated: {output_path}")

    if args.open:
        webbrowser.open(f"file://{Path(output_path).absolute()}")


def run_segmentation_command(args):
    """Handle the segmentation command."""
    from openadapt_viewer.viewers.segmentation_generator import generate_segmentation_viewer

    print("Generating catalog-enabled segmentation viewer...")

    output_path = generate_segmentation_viewer(
        output_path=str(args.output),
        auto_load_recording=args.auto_load,
    )

    print(f"Generated: {output_path}")

    if args.open:
        webbrowser.open(f"file://{Path(output_path).absolute()}")


def run_capture_command(args):
    """Handle the capture command."""
    from openadapt_viewer.viewers.capture import generate_capture_html

    print("Generating capture viewer...")

    # Create sample data for demo purposes
    # In real usage, this would load actual capture data
    sample_steps = [
        {
            "screenshot": None,
            "action": {"type": "click", "x": 0.85, "y": 0.05, "description": "Click System Settings icon"},
            "timestamp": 0,
            "duration": 1.167,
        },
        {
            "screenshot": None,
            "action": {"type": "click", "x": 0.15, "y": 0.3, "description": "Click Displays"},
            "timestamp": 1.167,
            "duration": 1.943,
        },
        {
            "screenshot": None,
            "action": {"type": "scroll", "direction": "down", "amount": 200},
            "timestamp": 3.110,
            "duration": 0.794,
        },
        {
            "screenshot": None,
            "action": {"type": "click", "x": 0.7, "y": 0.45, "description": "Click Night Shift"},
            "timestamp": 3.904,
            "duration": 1.048,
        },
        {
            "screenshot": None,
            "action": {"type": "click", "x": 0.8, "y": 0.35, "description": "Toggle Night Shift off"},
            "timestamp": 4.952,
            "duration": 1.793,
        },
    ]

    output_path = generate_capture_html(
        capture_id=args.capture_id,
        goal=args.goal or "Capture playback viewer",
        steps=sample_steps,
        output_path=args.output,
    )

    print(f"Generated: {output_path}")

    if args.open:
        webbrowser.open(f"file://{Path(output_path).absolute()}")


def run_demo_command(args):
    """Handle the demo command."""
    from openadapt_viewer.viewers.benchmark.data import create_sample_data

    print(f"Generating demo viewer with {args.tasks} sample tasks...")
    run_data = create_sample_data(num_tasks=args.tasks)

    output_path = generate_benchmark_html(
        output_path=args.output,
        standalone=False,
        run_data=run_data,
    )
    print(f"Generated: {output_path}")

    if args.open:
        webbrowser.open(f"file://{Path(output_path).absolute()}")


def run_screenshots_command(args):
    """Handle the screenshots command."""
    import subprocess

    if not args.screenshots_command:
        print("Error: No screenshots subcommand specified", file=sys.stderr)
        print("Use 'openadapt-viewer screenshots --help' to see available commands")
        sys.exit(1)

    if args.screenshots_command == "segmentation":
        # Build command to run the screenshot generation script
        script_path = Path(__file__).parent.parent.parent / "scripts" / "generate_segmentation_screenshots.py"

        if not script_path.exists():
            print(f"Error: Screenshot script not found: {script_path}", file=sys.stderr)
            sys.exit(1)

        # Build arguments
        cmd_args = [sys.executable, str(script_path)]

        if args.output:
            cmd_args.extend(["--output", str(args.output)])
        if args.viewer:
            cmd_args.extend(["--viewer", str(args.viewer)])
        if args.test_data:
            cmd_args.extend(["--test-data", str(args.test_data)])
        if args.skip_responsive:
            cmd_args.append("--skip-responsive")
        if args.save_metadata:
            cmd_args.append("--save-metadata")

        # Run the script
        print("Generating segmentation viewer screenshots...")
        # check=False: the exit status is propagated on the next line, so a
        # non-zero return is handled here rather than raised.
        result = subprocess.run(cmd_args, check=False)
        sys.exit(result.returncode)


def run_catalog_command(args):
    """Handle catalog commands."""
    from datetime import datetime

    from openadapt_viewer.catalog import get_catalog
    from openadapt_viewer.scanner import RecordingScanner, scan_and_update_catalog

    if not args.catalog_command:
        print("Error: No catalog subcommand specified", file=sys.stderr)
        print("Use 'openadapt-viewer catalog --help' to see available commands")
        sys.exit(1)

    catalog = get_catalog()

    if args.catalog_command == "scan":
        print("Scanning for recordings and segmentation results...")

        capture_dirs = [str(p) for p in args.capture_dirs] if args.capture_dirs else None
        seg_dirs = [str(p) for p in args.segmentation_dirs] if args.segmentation_dirs else None

        counts = scan_and_update_catalog(
            catalog=catalog,
            capture_dirs=capture_dirs,
            segmentation_dirs=seg_dirs,
        )

        print(f"\nIndexed {counts['recordings']} recordings")
        print(f"Indexed {counts['segmentations']} segmentation results")
        print(f"\nCatalog location: {catalog.db_path}")

    elif args.catalog_command == "list":
        recordings = catalog.get_all_recordings()

        if not recordings:
            print("No recordings found in catalog")
            print("Run 'openadapt-viewer catalog scan' to index recordings")
            return

        if args.json:
            output = []
            for rec in recordings:
                rec_dict = rec.model_dump()
                if args.with_segmentations:
                    seg_results = catalog.get_segmentation_results(rec.id)
                    rec_dict["segmentations"] = [s.model_dump() for s in seg_results]
                output.append(rec_dict)
            print(json_module.dumps(output, indent=2))
        else:
            print(f"\nFound {len(recordings)} recordings:\n")
            for rec in recordings:
                created = (
                    datetime.fromtimestamp(rec.created_at)
                    .astimezone()
                    .strftime("%Y-%m-%d %H:%M")
                )
                duration = f"{rec.duration_seconds:.1f}s" if rec.duration_seconds else "N/A"
                frames = rec.frame_count if rec.frame_count else "N/A"

                print(f"  {rec.name}")
                print(f"    ID: {rec.id}")
                print(f"    Path: {rec.path}")
                print(f"    Created: {created}")
                print(f"    Duration: {duration}")
                print(f"    Frames: {frames}")

                if args.with_segmentations:
                    seg_results = catalog.get_segmentation_results(rec.id)
                    if seg_results:
                        print(f"    Segmentations: {len(seg_results)}")
                        for seg in seg_results:
                            print(f"      - {seg.episode_count} episodes, {seg.boundary_count} boundaries")
                    else:
                        print("    Segmentations: None")

                print()

    elif args.catalog_command == "stats":
        stats = catalog.get_stats()

        print("\nCatalog Statistics:")
        print(f"  Recordings: {stats['recording_count']}")
        print(f"  Segmentation Results: {stats['segmentation_count']}")
        print(f"  Episodes: {stats['episode_count']}")
        print(f"\nDatabase: {stats['db_path']}")

    elif args.catalog_command == "clean":
        print("Cleaning missing entries from catalog...")
        removed = catalog.clean_missing()

        print(f"Removed {removed['recordings']} missing recordings")
        print(f"Removed {removed['segmentations']} missing segmentation results")

    elif args.catalog_command == "register":
        if not args.path.exists():
            print(f"Error: Path not found: {args.path}", file=sys.stderr)
            sys.exit(1)

        scanner = RecordingScanner(catalog)
        recording_id = args.path.name

        print(f"Registering recording: {args.path}")

        try:
            recording = scanner._extract_recording_info(args.path, recording_id)
            if args.name:
                recording.name = args.name

            # Recording names the field `id`; register_recording takes it as
            # `recording_id`. Splatting the model straight in raised TypeError,
            # which this except clause does not catch, so `catalog register`
            # ended in a traceback for every directory that got this far.
            fields = recording.model_dump()
            fields["recording_id"] = fields.pop("id")
            catalog.register_recording(**fields)
            print(f"Successfully registered: {recording.name}")
            print(f"  ID: {recording.id}")
            print(f"  Frames: {recording.frame_count}")
            print(f"  Events: {recording.event_count}")
        except (OSError, sqlite3.Error, ValueError) as e:
            # OSError: an unreadable recording directory, or one holding no
            # recording.db. sqlite3.Error: a corrupt recording.db or a failed
            # catalog write. ValueError: a row that does
            # not validate as a Recording. Anything else is a bug in this
            # package and should surface as a traceback rather than as a
            # one-line "Error registering recording".
            print(f"Error registering recording: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
