#!/usr/bin/env bash
#
# run-egress-blocked.sh — run a command with no route off this machine.
#
# Linux only. Starts the command in a new network namespace containing only
# loopback. Direct IP connections from the command and its children cannot
# reach networks outside that namespace. Services started inside it can use
# loopback to communicate with each other.
#
# Filesystem access is unchanged: pathname Unix sockets can reach processes
# outside the namespace, including brokers that forward network traffic. This
# is a direct-IP restriction, not a filesystem or untrusted-code sandbox.
#
# Before running the command, all interface and route inspections must succeed
# and confirm only loopback and no routes. The command runs as the invoking
# sudo user, with OPENADAPT_EGRESS_BLOCKED=netns as a test-location hint. Tests
# independently check the restriction rather than trusting that variable.
#
# Usage (as root). Pass every needed variable on the env command line; do not
# rely on sudo -E (GitHub-hosted runners ignore it):
#   sudo -n env "PATH=$PATH" "HOME=$HOME" KEY=VAL \
#     deploy/on-prem/bin/run-egress-blocked.sh CMD [ARGS...]
#
# Exit code: the command's, or 2 when the namespace cannot be set up.

set -euo pipefail

if [[ $# -eq 0 || "$1" == "-h" || "$1" == "--help" ]]; then
  grep '^#' "$0" | sed 's/^# \{0,1\}//'
  [[ $# -eq 0 ]] && exit 2 || exit 0
fi
if [[ "$(uname -s)" != "Linux" ]]; then
  echo "run-egress-blocked.sh: needs Linux network namespaces" >&2
  exit 2
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo "run-egress-blocked.sh: run as root (sudo); the command runs as the invoking user" >&2
  exit 2
fi
for tool in unshare ip setpriv; do
  command -v "$tool" >/dev/null || { echo "run-egress-blocked.sh: missing $tool" >&2; exit 2; }
done

RUN_UID="${SUDO_UID:-0}"
RUN_GID="${SUDO_GID:-0}"
export RUN_UID RUN_GID

# The inner script expands its own variables, inside the namespace.
# shellcheck disable=SC2016
exec unshare --net -- bash -euo pipefail -c '
  ip link set lo up
  if ! links="$(ip -o link show)"; then
    echo "run-egress-blocked.sh: cannot inspect interfaces" >&2
    exit 2
  fi
  interfaces="$(awk -F": " "{print \$2}" <<< "$links")"
  if [[ "$interfaces" != lo ]]; then
    echo "run-egress-blocked.sh: expected only loopback, found: $interfaces" >&2
    exit 2
  fi
  if ! routes4="$(ip route show)"; then
    echo "run-egress-blocked.sh: cannot inspect IPv4 routes" >&2
    exit 2
  fi
  if ! routes6="$(ip -6 route show)"; then
    echo "run-egress-blocked.sh: cannot inspect IPv6 routes" >&2
    exit 2
  fi
  if [[ -n "$routes4" || -n "$routes6" ]]; then
    echo "run-egress-blocked.sh: unexpected routes: $routes4 $routes6" >&2
    exit 2
  fi
  export OPENADAPT_EGRESS_BLOCKED=netns
  if [[ "$RUN_UID" == 0 ]]; then
    exec "$@"
  fi
  exec setpriv --reuid="$RUN_UID" --regid="$RUN_GID" --init-groups -- "$@"
' run-egress-blocked "$@"
