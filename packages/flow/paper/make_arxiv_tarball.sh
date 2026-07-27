#!/bin/sh
# Build a self-contained arXiv submission tarball for the OpenAdapt paper.
#
# arXiv compiles submissions itself, so the tarball must contain sources, not a
# PDF, and must NOT contain: a `build/` directory, `.bib` without a matching
# `.bbl` (arXiv does not run bibtex reliably), `.gitignore`, review files, or
# anything unreferenced by the document.
#
# Usage:
#   sh paper/make_arxiv_tarball.sh            # -> paper/dist/arxiv-main.tar.gz
#   sh paper/make_arxiv_tarball.sh workshop   # -> paper/dist/arxiv-workshop.tar.gz
#
# The script is fail-loud: it gate-checks the paper constants, does a clean
# build to produce the .bbl, verifies the staged tree compiles on its own from a
# scratch directory, and refuses to emit a tarball if any step fails.

set -eu

PAPER_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$PAPER_DIR/.." && pwd)
TARGET=${1:-main}
DIST="$PAPER_DIR/dist"

case "$TARGET" in
  main)     SRC_DIR="$PAPER_DIR";           OUT="$DIST/arxiv-main.tar.gz" ;;
  workshop) SRC_DIR="$PAPER_DIR/workshop";  OUT="$DIST/arxiv-workshop.tar.gz" ;;
  *) echo "usage: $0 [main|workshop]" >&2; exit 2 ;;
esac

echo "==> gate-checking paper constants against benchmark artifacts"
python3 "$PAPER_DIR/check_artifacts.py"

echo "==> clean build (produces the .bbl arXiv needs)"
make -C "$PAPER_DIR" clean >/dev/null 2>&1 || true
make -C "$PAPER_DIR" all

BBL="$SRC_DIR/build/main.bbl"
[ -f "$BBL" ] || { echo "FATAL: $BBL not produced" >&2; exit 1; }

STAGE=$(mktemp -d "${TMPDIR:-/tmp}/arxiv-stage.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT

echo "==> staging sources into $STAGE"
cp "$SRC_DIR/main.tex" "$STAGE/main.tex"
cp "$BBL" "$STAGE/main.bbl"
if [ "$TARGET" = "main" ]; then
  mkdir -p "$STAGE/sections"
  cp "$PAPER_DIR"/sections/*.tex "$STAGE/sections/"
fi

# Deliberately NOT staged: references.bib (superseded by the .bbl), Makefile,
# README.md, ARTIFACT_CHECKLIST.md, REVIEW_ADVERSARIAL*.md, check_artifacts.py,
# build/, dist/. arXiv only needs what the document \inputs.

echo "==> verifying the staged tree compiles standalone"
( cd "$STAGE" && pdflatex -interaction=nonstopmode -halt-on-error main.tex >/dev/null \
               && pdflatex -interaction=nonstopmode -halt-on-error main.tex >/dev/null )
if grep -qE "Warning: (Citation|Reference).*undefined" "$STAGE/main.log"; then
  echo "FATAL: staged tree has undefined citations or references" >&2
  grep -E "Warning: (Citation|Reference).*undefined" "$STAGE/main.log" >&2
  exit 1
fi
PAGES=$(grep -a "Output written" "$STAGE/main.log" | sed -E 's/.*\(([0-9]+) pages.*/\1/')
echo "    staged build OK: $PAGES pages"

# arXiv rejects auxiliary files; ship only sources plus the .bbl.
rm -f "$STAGE"/main.aux "$STAGE"/main.log "$STAGE"/main.out "$STAGE"/main.pdf

mkdir -p "$DIST"
rm -f "$OUT"
( cd "$STAGE" && COPYFILE_DISABLE=1 tar --exclude '.DS_Store' -czf "$OUT" . )

echo "==> wrote $OUT"
tar -tzf "$OUT" | sed 's/^/    /'
echo "    sha256: $(shasum -a 256 "$OUT" | cut -d' ' -f1)"
echo "    source commit: $(git -C "$REPO_ROOT" rev-parse HEAD)"
