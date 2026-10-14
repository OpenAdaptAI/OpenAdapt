"""Minimal benchmark viewer generator.

Generates standalone HTML files with embedded benchmark data.
"""

import json
from pathlib import Path
from typing import Any


def generate_minimal_viewer(run_data: dict[str, Any], output_path: str | Path) -> Path:
    """Generate minimal benchmark viewer with embedded data.

    Args:
        run_data: Benchmark run data (from summary.json)
        output_path: Path to write HTML file

    Returns:
        Path to generated HTML file
    """
    template_path = Path(__file__).parent / "minimal_viewer.html"
    html = template_path.read_text()

    # Embed data
    data_json = json.dumps({"runs": [run_data]}, indent=2)
    html = html.replace(
        'window.BENCHMARK_DATA = {\n            runs: []  // Will be populated by generator or API\n        };',
        f'window.BENCHMARK_DATA = {data_json};'
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)

    return output_path


def generate_from_benchmark_results(
    results_dir: str | Path,
    run_name: str,
    output_path: str | Path
) -> Path:
    """Generate viewer from benchmark_results directory.

    Args:
        results_dir: Path to benchmark_results directory
        run_name: Name of the run to display
        output_path: Path to write HTML file

    Returns:
        Path to generated HTML file
    """
    results_dir = Path(results_dir)
    run_dir = results_dir / run_name

    if not run_dir.exists():
        raise FileNotFoundError(f"Run directory not found: {run_dir}")

    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"Summary file not found: {summary_path}")

    run_data = json.loads(summary_path.read_text())
    return generate_minimal_viewer(run_data, output_path)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate minimal benchmark viewer")
    parser.add_argument(
        "--results-dir",
        type=str,
        default="benchmark_results",
        help="Path to benchmark_results directory"
    )
    parser.add_argument(
        "--run-name",
        type=str,
        required=True,
        help="Name of the run to display"
    )
    parser.add_argument(
        "--output",
        type=str,
        default="minimal_benchmark.html",
        help="Output HTML file path"
    )

    args = parser.parse_args()

    output = generate_from_benchmark_results(
        args.results_dir,
        args.run_name,
        args.output
    )
    print(f"Generated viewer: {output}")
