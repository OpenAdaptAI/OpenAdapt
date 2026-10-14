#!/usr/bin/env python3
"""
Generate segmentation viewer with embedded catalog data.

This script:
1. Scans for available episode files using the catalog system
2. Generates JavaScript catalog data
3. Embeds the catalog into segmentation_viewer.html
4. Outputs a standalone HTML file with automatic file discovery
"""

import argparse
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from openadapt_viewer.segmentation_catalog import generate_catalog_javascript


def generate_viewer_with_catalog(
    template_path: str,
    output_path: str,
    segmentation_dirs: list | None = None
):
    """
    Generate segmentation viewer with embedded catalog data.

    Args:
        template_path: Path to segmentation_viewer.html template
        output_path: Path to write the output HTML file
        segmentation_dirs: Directories to scan for episode files
    """
    # Read the template
    with open(template_path, "r") as f:
        template_html = f.read()

    # Generate catalog JavaScript
    catalog_js = generate_catalog_javascript(segmentation_dirs=segmentation_dirs)

    # Replace the placeholder catalog data with actual data
    # Find the catalog-data script tag and replace its content
    import re

    pattern = r'(<script id="catalog-data">)(.*?)(</script>)'
    replacement = r'\1\n' + catalog_js + r'\3'

    output_html = re.sub(pattern, replacement, template_html, flags=re.DOTALL)

    # Write the output
    with open(output_path, "w") as f:
        f.write(output_html)

    print(f"✓ Generated viewer with catalog: {output_path}")
    print(f"  Open in browser: file://{Path(output_path).resolve()}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate segmentation viewer with automatic file discovery"
    )
    parser.add_argument(
        "--template",
        default=str(Path(__file__).parent.parent / "segmentation_viewer.html"),
        help="Path to segmentation_viewer.html template",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="segmentation_viewer_with_catalog.html",
        help="Output HTML file path",
    )
    parser.add_argument(
        "--scan-dir",
        action="append",
        dest="scan_dirs",
        help="Additional directory to scan for episode files (can be specified multiple times)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the generated viewer in the default browser",
    )

    args = parser.parse_args()

    generate_viewer_with_catalog(
        template_path=args.template,
        output_path=args.output,
        segmentation_dirs=args.scan_dirs,
    )

    if args.open:
        import webbrowser
        webbrowser.open(f"file://{Path(args.output).resolve()}")


if __name__ == "__main__":
    main()
