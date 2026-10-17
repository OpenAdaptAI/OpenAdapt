#!/bin/sh
# OpenAdapt installer (browser quickstart) — https://github.com/OpenAdaptAI/openadapt-flow
#
#   curl -fsSL https://raw.githubusercontent.com/OpenAdaptAI/openadapt-flow/main/scripts/install.sh | sh
#
# Installs uv (a fast Python toolchain) if you don't have it, provisions
# Python 3.12 (downloading a managed interpreter only if no suitable system
# Python exists — 3.13+ is not supported yet), installs OpenAdapt with browser
# support as a persistent `openadapt` command, and finishes with a short
# environment check.
#
# Safe to re-run: it upgrades in place. Nothing runs with elevated privileges;
# read the script first if you like — that's why it's served in the clear over
# HTTPS.
set -eu

info() { printf '\033[1;36m==>\033[0m %s\n' "$1"; }
err()  { printf '\033[1;31mError:\033[0m %s\n' "$1" >&2; }

if ! command -v curl >/dev/null 2>&1; then
    err "curl is required but not installed."
    exit 1
fi

PYTHON_VERSION="3.12"

if ! command -v uv >/dev/null 2>&1; then
    info "Installing uv (fast Python package manager)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv installs to ~/.local/bin by default; make it visible to this script.
    export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v uv >/dev/null 2>&1; then
    err "uv was installed but isn't on your PATH yet."
    err "Open a new terminal and re-run this command, or add \$HOME/.local/bin to PATH."
    exit 1
fi

# The square brackets in 'openadapt[browser]' are glob characters in many
# shells — installing from here means nobody has to quote them by hand.
info "Installing OpenAdapt with browser support…"
uv tool install --upgrade --python "$PYTHON_VERSION" 'openadapt[browser]'

# Make sure the installed `openadapt` command is on PATH in future shells.
uv tool update-shell >/dev/null 2>&1 || true
export PATH="$HOME/.local/bin:$PATH"

# ---- environment check ----------------------------------------------------
os="$(uname -s)"
arch="$(uname -m 2>/dev/null || echo unknown)"
python_status="not found"
python_bin="$(uv python find "$PYTHON_VERSION" 2>/dev/null || true)"
if [ -n "$python_bin" ]; then
    python_status="$("$python_bin" --version 2>/dev/null || echo "$PYTHON_VERSION") at $python_bin"
fi
command_path="$(command -v openadapt 2>/dev/null || echo "not on PATH yet — open a new terminal first")"

printf '\n'
info "Environment"
printf '    OS:       %s (%s)\n' "$os" "$arch"
printf '    Python:   %s\n' "$python_status"
printf '    Command:  %s\n' "$command_path"
printf '    Browser:  Chromium provisions automatically on first browser use;\n'
printf '              nothing was downloaded during this install.\n'

info "OpenAdapt is installed. Run your first workflow:"
printf '\n    openadapt quickstart\n\n'
info "If your shell can't find it yet, open a new terminal first."
