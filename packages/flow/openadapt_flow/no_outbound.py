"""Strict no-outbound mode: refuse this process's connections off the machine.

Set ``OPENADAPT_NO_OUTBOUND=1`` and ``openadapt-flow`` installs a Python audit
hook (PEP 578) at startup. The hook sees every ``socket.connect``,
``socket.sendto`` and name lookup the interpreter makes, from any library,
and raises :class:`OutboundBlocked` before the packet is sent unless the peer
is on this machine:

* loopback addresses (``127.0.0.0/8``, ``::1``) and ``localhost``;
* Unix domain sockets;
* IP literals or CIDR ranges named in ``OPENADAPT_NO_OUTBOUND_ALLOW``
  (comma-separated), or listed in the JSON file named by
  ``OPENADAPT_NO_OUTBOUND_ALLOW_FILE`` (see :mod:`no_outbound_allow`, which
  derives that file from the remote-desktop session profile). Only private,
  loopback or link-local ranges are accepted, and never host names, so
  allowing a LAN host never needs a DNS query.

Other machines on the LAN are refused like any outside host. Nothing is
allowed by default except this machine.

Name lookups for any other host are refused too, because the DNS query
itself would carry the host name off the machine.

An audit hook cannot be removed once installed, so the mode holds for the
life of the process. ``OutboundBlocked`` subclasses ``PermissionError`` (an
``OSError``), so HTTP clients report it as a failed connection and code that
already fails closed on network errors keeps doing so.

Each refusal is appended as one JSON line to ``OPENADAPT_NO_OUTBOUND_LOG``
when that is set. The line holds the event name, the peer and the time,
never payload bytes.

Child processes are refused unless something contains them. The hook cannot
see a child's sockets, so starting one (``subprocess``, ``os.exec*``,
``os.posix_spawn``, ``os.spawn*``, ``os.system``) raises
:class:`OutboundBlocked` unless one of these holds:

* the child is a Python interpreter or the ``openadapt-flow`` command and
  keeps ``OPENADAPT_NO_OUTBOUND`` in its environment, so it installs the same
  hook when it imports ``openadapt_flow``;
* the child is one of the local system tools in :data:`LOCAL_TOOLS` that the
  on-prem service runs (``ps``, ``lsof`` and the like), which open no network
  connections;
* ``OPENADAPT_NO_OUTBOUND_CHILDREN`` names what contains every child at the
  kernel or site level (one of :data:`CONTAINMENTS`, for example ``pf`` when
  the on-prem installer's firewall rules are loaded).

Limits: the hook covers this Python process only. Native code that opens
sockets without Python's ``socket`` module is not covered. A Python child
that never imports ``openadapt_flow`` does not install the hook. A child
allowed by a named containment is only as contained as that firewall. A
service on this machine that the allowed loopback addresses reach (a proxy,
an SSH tunnel, a browser's debugging port) can forward traffic off the
machine. ``openadapt-flow service egress-audit`` samples the whole process
tree from the operating system's socket table, and the site firewall stays
the primary control.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys
import threading
import time
from typing import Any, Mapping, Optional, Sequence, Union

ENV_MODE = "OPENADAPT_NO_OUTBOUND"
ENV_ALLOW = "OPENADAPT_NO_OUTBOUND_ALLOW"
ENV_LOG = "OPENADAPT_NO_OUTBOUND_LOG"
ENV_ALLOW_FILE = "OPENADAPT_NO_OUTBOUND_ALLOW_FILE"
ENV_CHILDREN = "OPENADAPT_NO_OUTBOUND_CHILDREN"

#: Values ``OPENADAPT_NO_OUTBOUND_CHILDREN`` accepts: what contains every child
#: process. Naming one is the operator's statement; Flow does not check it.
CONTAINMENTS = frozenset(
    {"pf", "systemd-ipaddressdeny", "windows-firewall", "site-firewall", "sandbox"}
)

#: Local system tools the on-prem service runs in strict mode. None of them
#: opens a network connection.
LOCAL_TOOLS = frozenset(
    {"ps", "lsof", "stat", "sysctl", "launchctl", "pfctl", "sw_vers", "uname"}
)

_TRUE = {"1", "true", "on", "yes", "strict"}
_LOCAL_NAMES = {"localhost", "localhost.", "ip6-localhost"}

Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

_lock = threading.Lock()
_installed = False
_allow: tuple[Network, ...] = ()
_log_path: Optional[str] = None
_children: Optional[str] = None
_blocked_count = 0


class OutboundBlocked(PermissionError):
    """A connection or lookup would have left the machine in no-outbound mode."""


def enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when ``OPENADAPT_NO_OUTBOUND`` asks for strict no-outbound mode."""
    env = os.environ if env is None else env
    return str(env.get(ENV_MODE, "")).strip().lower() in _TRUE


def installed() -> bool:
    """True once the audit hook is active in this process."""
    return _installed


def blocked_count() -> int:
    """How many connections or lookups this process has refused."""
    return _blocked_count


def parse_allow(value: Optional[str]) -> tuple[Network, ...]:
    """Parse ``OPENADAPT_NO_OUTBOUND_ALLOW``. Raises ``ValueError`` on bad input.

    Accepts IP literals and CIDR ranges that are private, loopback or
    link-local. A host name or a public range is refused: allowing it would
    let traffic leave the site.
    """
    nets: list[Network] = []
    for raw in (value or "").split(","):
        item = raw.strip()
        if not item:
            continue
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError as exc:
            raise ValueError(
                f"{ENV_ALLOW} takes IP addresses or CIDR ranges, not {item!r}"
            ) from exc
        if not (net.is_private or net.is_loopback or net.is_link_local):
            raise ValueError(
                f"{ENV_ALLOW} refuses public range {item!r}: it would leave the site"
            )
        nets.append(net)
    return tuple(nets)


def allow_from_env(env: Optional[Mapping[str, str]] = None) -> tuple[Network, ...]:
    """Every allowed range: ``OPENADAPT_NO_OUTBOUND_ALLOW`` plus the allow file.

    Raises ``ValueError`` when either is invalid, or when the allow file is
    named but missing.
    """
    env = os.environ if env is None else env
    nets = list(parse_allow(env.get(ENV_ALLOW)))
    path = env.get(ENV_ALLOW_FILE)
    if path:
        from pathlib import Path

        from openadapt_flow.no_outbound_allow import AllowList

        nets.extend(AllowList.load(Path(path)).networks())
    return tuple(nets)


def parse_children(value: Optional[str]) -> Optional[str]:
    """Parse ``OPENADAPT_NO_OUTBOUND_CHILDREN``. Raises ``ValueError`` on bad input."""
    item = (value or "").strip().lower()
    if not item:
        return None
    if item not in CONTAINMENTS:
        raise ValueError(
            f"{ENV_CHILDREN} names what contains child processes; it takes one "
            f"of {', '.join(sorted(CONTAINMENTS))}, not {value!r}"
        )
    return item


def children_containment() -> Optional[str]:
    """The containment named for child processes in this process, if any."""
    return _children


def _is_python(path: str) -> bool:
    name = os.path.basename(path).lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if name in ("openadapt-flow", "openadapt"):
        return True
    if path == sys.executable:
        return True
    rest = name[len("python") :] if name.startswith("python") else None
    return rest is not None and all(ch.isdigit() or ch == "." for ch in rest)


def _keeps_strict(env: Any) -> bool:
    if env is None:
        return enabled()
    try:
        items = env.items()
    except AttributeError:
        return False
    for key, value in items:
        if isinstance(key, bytes):
            key = key.decode("utf-8", "replace")
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        if key == ENV_MODE:
            return str(value).strip().lower() in _TRUE
    return False


def child_is_covered(path: Any, env: Any) -> bool:
    """True if a child at ``path`` with ``env`` stays inside no-outbound mode."""
    if _children is not None:
        return True
    if isinstance(path, bytes):
        path = path.decode("utf-8", "replace")
    if not isinstance(path, (str, os.PathLike)):
        return False
    path = os.fspath(path)
    if _is_python(path):
        return _keeps_strict(env)
    return os.path.basename(path) in LOCAL_TOOLS


def _ip(host: Any) -> Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return None
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def host_is_local(host: Any, allow: Sequence[Network] = ()) -> bool:
    """True if ``host`` is this machine or an explicitly allowed LAN range."""
    if host is None or host in ("", b""):
        return True  # getaddrinfo(None, port): the local wildcard, no lookup
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if isinstance(host, str) and host.lower() in _LOCAL_NAMES:
        return True
    addr = _ip(host)
    if addr is None:
        return False  # a host name: resolving it would send a DNS query
    if addr.is_loopback or addr.is_unspecified:
        return True
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None and mapped.is_loopback:
        return True
    return any(addr in net for net in allow)


def _record(event: str, host: Any, port: Any) -> None:
    global _blocked_count
    _blocked_count += 1
    if not _log_path:
        return
    line = json.dumps(
        {
            "event": event,
            "host": host if isinstance(host, (str, int, type(None))) else repr(host),
            "port": port if isinstance(port, (int, str, type(None))) else repr(port),
            "pid": os.getpid(),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        sort_keys=True,
    )
    try:
        fd = os.open(_log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass  # the refusal still stands; a log failure never opens a path


def _refuse_child(event: str, path: Any) -> None:
    _record(event, path, None)
    raise OutboundBlocked(
        f"no-outbound mode refused {event} of {path!r}: this hook cannot see a "
        "child process's sockets. Contain child processes at the kernel or "
        f"site level and name it in {ENV_CHILDREN} "
        f"({', '.join(sorted(CONTAINMENTS))})."
    )


def _first_arg(args: Any) -> Any:
    if isinstance(args, (list, tuple)) and args:
        return args[0]
    return None


def _refuse(event: str, host: Any, port: Any) -> None:
    _record(event, host, port)
    raise OutboundBlocked(
        f"no-outbound mode refused {event} to {host!r}"
        + (f" port {port}" if port not in (None, "") else "")
    )


def _peer(address: Any) -> "tuple[Any, Any]":
    if isinstance(address, tuple) and address:
        return address[0], address[1] if len(address) > 1 else None
    return address, None


def _hook(event: str, args: tuple) -> None:
    if event == "subprocess.Popen":
        executable, argv, _cwd, env = args
        path = executable if executable is not None else _first_arg(argv)
        if not child_is_covered(path, env):
            _refuse_child(event, path)
        return
    if event in ("os.exec", "os.posix_spawn"):
        if not child_is_covered(args[0], args[2]):
            _refuse_child(event, args[0])
        return
    if event == "os.spawn":
        if not child_is_covered(args[1], args[3]):
            _refuse_child(event, args[1])
        return
    if event in ("os.system", "os.startfile"):
        # A shell command line or a file opened by its associated program:
        # nothing here says which program runs.
        if _children is None:
            _refuse_child(event, args[0])
        return
    if event in ("socket.connect", "socket.sendto"):
        sock, address = args[0], args[1]
        family = getattr(sock, "family", None)
        if family == getattr(socket, "AF_UNIX", object()):
            return
        host, port = _peer(address)
        if not host_is_local(host, _allow):
            _refuse(event, host, port)
    elif event == "socket.getaddrinfo":
        host, port = args[0], args[1]
        if not host_is_local(host, _allow):
            _refuse(event, host, port)
    elif event in (
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.gethostbyaddr",
        "socket.getnameinfo",
    ):
        target = args[0]
        host, port = _peer(target) if isinstance(target, tuple) else (target, None)
        if not host_is_local(host, _allow):
            _refuse(event, host, port)


def enforce(env: Optional[Mapping[str, str]] = None) -> None:
    """Install the no-outbound audit hook in this process. Idempotent.

    Raises ``ValueError`` if ``OPENADAPT_NO_OUTBOUND_ALLOW`` or the allow file
    is invalid, so a mistyped allow list stops startup instead of widening the
    mode.

    A later call can name a containment for child processes
    (``OPENADAPT_NO_OUTBOUND_CHILDREN``) when none was named yet, as the
    on-prem service does once it has read its firewall record. A named
    containment is never changed or removed.
    """
    global _installed, _allow, _log_path, _children
    env = os.environ if env is None else env
    allow = allow_from_env(env)
    children = parse_children(env.get(ENV_CHILDREN))
    with _lock:
        if _children is None:
            _children = children
        if _installed:
            return
        _allow = allow
        _log_path = env.get(ENV_LOG) or None
        sys.addaudithook(_hook)
        _installed = True


def enforce_from_env(env: Optional[Mapping[str, str]] = None) -> bool:
    """Install the hook when the environment asks for it. Returns whether it did."""
    if not enabled(env):
        return False
    enforce(env)
    return True
