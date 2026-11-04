#!/usr/bin/env bash
#
# run-egress-blocked.sh — run a command with no route off this machine.
#
# Linux only. Starts the command in a new network namespace that holds only
# the loopback interface, so the kernel refuses every connection and DNS query
# that would leave the machine, from Python, from native code, and from child
# processes alike (a browser included). Loopback still works, so a local
# system of record on 127.0.0.1 stays reachable.
#
# Before running the command it checks that the namespace really has no
# interface besides loopback and no route, and refuses to run otherwise. The
# command runs as the user who invoked sudo, with OPENADAPT_EGRESS_BLOCKED=netns
# in its environment. That variable only tells a test where it is running; the
# test still has to show for itself that nothing leaves the machine.
#
# Usage (as root, normally through sudo -E so PATH and HOME are kept):
#   sudo -E env "PATH=$PATH" deploy/on-prem/bin/run-egress-blocked.sh CMD [ARGS...]
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
  others="$(ip -o link show | awk -F": " "{print \$2}" | grep -v "^lo$" || true)"
  if [[ -n "$others" ]]; then
    echo "run-egress-blocked.sh: interfaces besides loopback: $others" >&2
    exit 2
  fi
  routes="$(ip route show; ip -6 route show | grep -v "^::1 " | grep -v "^fe80::" || true)"
  if [[ -n "$routes" ]]; then
    echo "run-egress-blocked.sh: unexpected routes: $routes" >&2
    exit 2
  fi
  export OPENADAPT_EGRESS_BLOCKED=netns
  if [[ "$RUN_UID" == 0 ]]; then
    exec "$@"
  fi
  exec setpriv --reuid="$RUN_UID" --regid="$RUN_GID" --init-groups -- "$@"
' run-egress-blocked "$@"
