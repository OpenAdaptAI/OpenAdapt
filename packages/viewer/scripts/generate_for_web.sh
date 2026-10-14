#!/bin/bash
# Generate screenshots for openadapt-web landing page
#
# This script generates optimized screenshots from openadapt-viewer
# and copies them to openadapt-web for deployment.
#
# Usage:
#   ./scripts/generate_for_web.sh [--help]
#
# Requirements:
#   - openadapt-viewer and openadapt-web repos cloned
#   - uv installed
#   - Playwright installed (uv run playwright install chromium)

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VIEWER_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Default paths
TMP_DIR="/tmp/oa-screenshots"
WEB_DIR="${WEB_DIR:-$VIEWER_ROOT/../openadapt-web/public/images/screenshots}"

# Parse arguments
HELP=false
for arg in "$@"; do
    case $arg in
        --help|-h)
            HELP=true
            ;;
        --web-dir=*)
            WEB_DIR="${arg#*=}"
            ;;
        *)
            echo -e "${RED}Unknown argument: $arg${NC}"
            exit 1
            ;;
    esac
done

if [ "$HELP" = true ]; then
    echo "Generate screenshots for openadapt-web landing page"
    echo ""
    echo "Usage: $0 [options]"
    echo ""
    echo "Options:"
    echo "  --web-dir=PATH    Path to openadapt-web/public/images/screenshots"
    echo "                    (default: ../openadapt-web/public/images/screenshots)"
    echo "  -h, --help        Show this help message"
    echo ""
    echo "Example:"
    echo "  $0 --web-dir=/path/to/openadapt-web/public/images/screenshots"
    exit 0
fi

echo -e "${GREEN}===================================================${NC}"
echo -e "${GREEN}OpenAdapt Web Screenshot Generator${NC}"
echo -e "${GREEN}===================================================${NC}"
echo ""

# Check if openadapt-web directory exists
if [ ! -d "$(dirname "$WEB_DIR")" ]; then
    echo -e "${RED}Error: openadapt-web directory not found${NC}"
    echo -e "${YELLOW}Expected: $WEB_DIR${NC}"
    echo ""
    echo "Please ensure openadapt-web is cloned adjacent to openadapt-viewer:"
    echo "  <your checkout root>/"
    echo "    ├── openadapt-viewer/"
    echo "    └── openadapt-web/"
    echo ""
    echo "Or specify the path with --web-dir=PATH"
    exit 1
fi

echo -e "Viewer root: ${YELLOW}$VIEWER_ROOT${NC}"
echo -e "Temp directory: ${YELLOW}$TMP_DIR${NC}"
echo -e "Web directory: ${YELLOW}$WEB_DIR${NC}"
echo ""

# Create temp directory
echo -e "${GREEN}Creating temp directory...${NC}"
rm -rf "$TMP_DIR"
mkdir -p "$TMP_DIR"

cd "$VIEWER_ROOT"

# Check dependencies
echo -e "${GREEN}Checking dependencies...${NC}"
if ! command -v uv &> /dev/null; then
    echo -e "${RED}Error: uv not found${NC}"
    echo "Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# Check Playwright
if ! uv run python scripts/generate_comprehensive_screenshots.py --check-deps &> /dev/null; then
    echo -e "${YELLOW}Warning: Playwright may not be installed${NC}"
    echo "Run: uv pip install playwright && uv run playwright install chromium"
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

echo ""
echo -e "${GREEN}===================================================${NC}"
echo -e "${GREEN}Generating Screenshots${NC}"
echo -e "${GREEN}===================================================${NC}"
echo ""

# Generate capture viewer screenshots (if HTML files exist)
if [ -f "$VIEWER_ROOT/temp/turn-off-nightshift_viewer.html" ] || \
   [ -f "$VIEWER_ROOT/temp/demo_new_viewer.html" ]; then
    echo -e "${GREEN}[1/3] Capture Viewer Screenshots${NC}"
    uv run python scripts/generate_comprehensive_screenshots.py \
        --viewers capture \
        --output "$TMP_DIR/capture" || true  # Don't fail if HTML files missing
    echo ""
else
    echo -e "${YELLOW}[1/3] Capture Viewer - SKIPPED (no HTML files in temp/)${NC}"
    echo "To generate capture viewer screenshots:"
    echo "  1. Create recordings in openadapt-capture"
    echo "  2. Generate viewer HTML files"
    echo "  3. Place in $VIEWER_ROOT/temp/"
    echo ""
fi

# Generate segmentation viewer screenshots
if [ -f "$VIEWER_ROOT/segmentation_viewer.html" ] && \
   [ -f "$VIEWER_ROOT/test_episodes.json" ]; then
    echo -e "${GREEN}[2/3] Segmentation Viewer Screenshots${NC}"
    uv run python scripts/generate_segmentation_screenshots.py \
        --skip-responsive \
        --output "$TMP_DIR/segmentation"
    echo ""
else
    echo -e "${YELLOW}[2/3] Segmentation Viewer - SKIPPED (missing HTML or test data)${NC}"
    echo "Required files:"
    echo "  - $VIEWER_ROOT/segmentation_viewer.html"
    echo "  - $VIEWER_ROOT/test_episodes.json"
    echo ""
fi

# Generate benchmark viewer screenshots (if exists)
if [ -f "$VIEWER_ROOT/benchmark_viewer.html" ]; then
    echo -e "${GREEN}[3/3] Benchmark Viewer Screenshots${NC}"
    echo -e "${YELLOW}Note: Benchmark viewer screenshot generation not yet implemented${NC}"
    echo -e "${YELLOW}Manually add benchmark screenshots to $TMP_DIR/benchmark/${NC}"
    echo ""
else
    echo -e "${YELLOW}[3/3] Benchmark Viewer - SKIPPED (no HTML file)${NC}"
    echo ""
fi

# Check if any screenshots were generated
SCREENSHOT_COUNT=$(find "$TMP_DIR" -name "*.png" 2>/dev/null | wc -l)
if [ "$SCREENSHOT_COUNT" -eq 0 ]; then
    echo -e "${RED}Error: No screenshots were generated${NC}"
    echo "Please check the error messages above."
    exit 1
fi

echo -e "${GREEN}Generated $SCREENSHOT_COUNT screenshots${NC}"
echo ""

# Copy to openadapt-web
echo -e "${GREEN}===================================================${NC}"
echo -e "${GREEN}Copying to openadapt-web${NC}"
echo -e "${GREEN}===================================================${NC}"
echo ""

mkdir -p "$WEB_DIR"
cp -rv "$TMP_DIR"/* "$WEB_DIR/"

# Generate metadata
echo -e "${GREEN}Generating metadata...${NC}"
cat > "$WEB_DIR/README.md" <<EOF
# Autogenerated Screenshots

These screenshots are automatically generated from openadapt-viewer.

**DO NOT EDIT MANUALLY** - Changes will be overwritten.

## Generation Info

- **Generated**: $(date -u +"%Y-%m-%d %H:%M:%S UTC")
- **Script**: openadapt-viewer/scripts/generate_for_web.sh
- **Source Repo**: https://github.com/OpenAdaptAI/openadapt-viewer
- **Screenshot Count**: $SCREENSHOT_COUNT

## Regenerating

To regenerate these screenshots:

\`\`\`bash
cd /path/to/openadapt-viewer
./scripts/generate_for_web.sh
\`\`\`

Or with custom web directory:

\`\`\`bash
./scripts/generate_for_web.sh --web-dir=/path/to/openadapt-web/public/images/screenshots
\`\`\`

## Screenshots by Viewer

EOF

# List screenshots by directory
for viewer_dir in "$WEB_DIR"/*; do
    if [ -d "$viewer_dir" ]; then
        viewer_name=$(basename "$viewer_dir")
        echo "### $viewer_name" >> "$WEB_DIR/README.md"
        echo "" >> "$WEB_DIR/README.md"
        find "$viewer_dir" -name "*.png" -exec basename {} \; | sort | while read -r file; do
            size=$(du -h "$viewer_dir/$file" | cut -f1)
            echo "- $file ($size)" >> "$WEB_DIR/README.md"
        done
        echo "" >> "$WEB_DIR/README.md"
    fi
done

echo ""
echo -e "${GREEN}===================================================${NC}"
echo -e "${GREEN}Summary${NC}"
echo -e "${GREEN}===================================================${NC}"
echo ""
echo -e "Screenshots: ${GREEN}$SCREENSHOT_COUNT${NC}"
echo -e "Location: ${YELLOW}$WEB_DIR${NC}"
echo ""

# Calculate total size
TOTAL_SIZE=$(du -sh "$WEB_DIR" | cut -f1)
echo -e "Total size: ${YELLOW}$TOTAL_SIZE${NC}"
echo ""

# List generated files
echo -e "${GREEN}Generated files:${NC}"
find "$WEB_DIR" -name "*.png" -exec du -h {} \; | sort -k2 | while read -r size file; do
    echo -e "  ${YELLOW}$size${NC}  $(basename "$file")"
done
echo ""

echo -e "${GREEN}===================================================${NC}"
echo -e "${GREEN}Next Steps${NC}"
echo -e "${GREEN}===================================================${NC}"
echo ""
echo "1. Review the generated screenshots:"
echo -e "   ${YELLOW}open $WEB_DIR${NC}"
echo ""
echo "2. Commit to openadapt-web (via PR):"
echo -e "   ${YELLOW}cd $(dirname $(dirname "$WEB_DIR"))${NC}"
echo -e "   ${YELLOW}git checkout -b update-screenshots-$(date +%Y%m%d)${NC}"
echo -e "   ${YELLOW}git add public/images/screenshots/${NC}"
echo -e "   ${YELLOW}git commit -m \"Update autogenerated screenshots\"${NC}"
echo -e "   ${YELLOW}git push -u origin update-screenshots-$(date +%Y%m%d)${NC}"
echo -e "   ${YELLOW}gh pr create --title \"Update autogenerated screenshots\" --body \"Updated from openadapt-viewer\"${NC}"
echo ""
echo "3. Review PR and merge to deploy"
echo ""

echo -e "${GREEN}✓ Screenshot generation completed!${NC}"
