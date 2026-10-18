"""The no-outbound allow list, derived from the remote-desktop session profile.

Strict no-outbound mode refuses every peer that is not this machine, the LAN
included. A deployment that drives an application over a remote-desktop
session needs exactly one exception: the session host (and its gateway, when
one is configured). This module keeps that exception small and visible.

* :func:`targets_from_file` reads the host(s) a session connects to from a
  FreeRDP session file (JSON, see :func:`targets_from_session_data`) or a
  Microsoft ``.rdp`` file (``full address``, ``alternate full address``,
  ``gatewayhostname``).
* :func:`derive` turns those hosts into single-address allow entries. A host
  name is resolved once, at install time, before strict mode is on. Public
  addresses are refused: allowing one would let traffic leave the site.
* :class:`AllowList` is the JSON file named by
  ``OPENADAPT_NO_OUTBOUND_ALLOW_FILE``. Each entry records where it came from
  (``derived`` from which file, or ``manual``), so an operator can see why an
  address is allowed and remove it.

:func:`openadapt_flow.no_outbound.enforce` reads the file at startup together
with ``OPENADAPT_NO_OUTBOUND_ALLOW``. A file that is named but missing or
invalid stops startup, so a broken allow list never widens the mode.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

ALLOW_SCHEMA = "openadapt.no-outbound-allow/v1"
DEFAULT_RDP_PORT = 3389

#: host -> list of IP strings. Injected by tests.
Resolver = Callable[[str], list[str]]


class AllowListError(ValueError):
    """The allow list or the session profile it came from is unusable."""


@dataclass(frozen=True)
class SessionTarget:
    """One host a remote-desktop session connects to."""

    host: str
    port: int
    role: str  # session | alternate | gateway
    source: str  # file the host was read from


@dataclass(frozen=True)
class AllowEntry:
    """One allowed address. ``port`` is advisory: the in-process hook allows
    the whole address; the firewall rules, when installed, use the port."""

    network: str
    origin: str  # derived | manual
    host: Optional[str] = None
    port: Optional[int] = None
    role: Optional[str] = None
    source: Optional[str] = None
    added_at: str = ""

    def as_network(self) -> "ipaddress.IPv4Network | ipaddress.IPv6Network":
        return ipaddress.ip_network(self.network, strict=False)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _split_host_port(value: str, default_port: int) -> tuple[str, int]:
    value = value.strip()
    if value.startswith("["):  # [v6]:port
        host, _, rest = value[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif value.count(":") == 1:
        host, port = value.split(":", 1)
    else:  # bare host or bare IPv6 literal
        host, port = value, ""
    host = host.strip()
    if not host:
        raise AllowListError(f"no host in {value!r}")
    try:
        return host, int(port) if port else default_port
    except ValueError as exc:
        raise AllowListError(f"bad port in {value!r}") from exc


def targets_from_rdp_text(text: str, source: str = "<rdp>") -> list[SessionTarget]:
    """Hosts named by a Microsoft ``.rdp`` file.

    Reads ``full address``, ``alternate full address``, ``server port`` and,
    unless ``gatewayusagemethod`` is 0, ``gatewayhostname``.
    """
    fields: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        parts = line.split(":", 2)
        if len(parts) == 3:
            fields[parts[0].strip().lower()] = parts[2].strip()
    port = DEFAULT_RDP_PORT
    if fields.get("server port", "").isdigit():
        port = int(fields["server port"])
    out: list[SessionTarget] = []
    for key, role in (
        ("full address", "session"),
        ("alternate full address", "alternate"),
    ):
        if fields.get(key):
            host, p = _split_host_port(fields[key], port)
            out.append(SessionTarget(host, p, role, source))
    gateway = fields.get("gatewayhostname", "")
    if gateway and fields.get("gatewayusagemethod", "1") != "0":
        host, p = _split_host_port(gateway, 443)
        out.append(SessionTarget(host, p, "gateway", source))
    if not any(t.role == "session" for t in out):
        raise AllowListError(f"{source}: no 'full address' line")
    return _dedupe(out)


def targets_from_session_data(
    data: Mapping[str, Any], source: str = "<session>"
) -> list[SessionTarget]:
    """Hosts named by a FreeRDP session file.

    The file is JSON with a ``target`` object holding ``host`` and optional
    ``port`` (the fields of ``backends.xfreerdp.RdpTarget``), an optional
    ``profile`` object (``SessionProfile``), and an optional ``gateway``
    object with ``host`` and ``port``.
    """
    target = data.get("target")
    if not isinstance(target, Mapping) or not isinstance(target.get("host"), str):
        raise AllowListError(f"{source}: no target.host")
    out = [
        SessionTarget(
            *_split_host_port(
                str(target["host"]), int(target.get("port") or DEFAULT_RDP_PORT)
            ),
            "session",
            source,
        )
    ]
    gateway = data.get("gateway")
    if isinstance(gateway, Mapping) and isinstance(gateway.get("host"), str):
        out.append(
            SessionTarget(
                *_split_host_port(
                    str(gateway["host"]), int(gateway.get("port") or 443)
                ),
                "gateway",
                source,
            )
        )
    return _dedupe(out)


def targets_from_file(path: Path) -> list[SessionTarget]:
    """Read the session hosts from a ``.rdp`` file or a FreeRDP session JSON."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AllowListError(f"cannot read session profile {path}: {exc}") from exc
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16")  # .rdp files saved by Windows
    else:
        text = raw.decode("utf-8", "replace")
    if path.suffix.lower() == ".rdp" or not text.lstrip().startswith("{"):
        return targets_from_rdp_text(text, str(path))
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AllowListError(f"{path}: not valid JSON") from exc
    if not isinstance(data, Mapping):
        raise AllowListError(f"{path}: expected a JSON object")
    return targets_from_session_data(data, str(path))


def _dedupe(targets: Iterable[SessionTarget]) -> list[SessionTarget]:
    seen, out = set(), []
    for t in targets:
        key = (t.host.lower(), t.port, t.role)
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def _default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _check_private(addr: str, host: str) -> str:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    if not (ip.is_private or ip.is_loopback or ip.is_link_local):
        raise AllowListError(
            f"{host} resolves to public address {ip}; strict no-outbound mode "
            "allows only addresses on the site network"
        )
    return str(ipaddress.ip_network(ip))


def entries_for_targets(
    targets: Sequence[SessionTarget], resolver: Optional[Resolver] = None
) -> list[AllowEntry]:
    """One single-address entry per address each target resolves to."""
    resolve = resolver or _default_resolver
    now = _utc()
    out: list[AllowEntry] = []
    for t in targets:
        try:
            ipaddress.ip_address(t.host.split("%", 1)[0])
            addrs = [t.host]
        except ValueError:
            try:
                addrs = resolve(t.host)
            except OSError as exc:
                raise AllowListError(f"cannot resolve {t.host}: {exc}") from exc
            if not addrs:
                raise AllowListError(f"{t.host} resolved to no address")
        for addr in addrs:
            out.append(
                AllowEntry(
                    network=_check_private(addr, t.host),
                    origin="derived",
                    host=t.host,
                    port=t.port,
                    role=t.role,
                    source=t.source,
                    added_at=now,
                )
            )
    return out


@dataclass
class AllowList:
    """The allow-list file. Entries are unique by (network, port)."""

    entries: list[AllowEntry] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "AllowList":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AllowListError(f"allow-list file not found: {path}") from exc
        except (OSError, ValueError) as exc:
            raise AllowListError(f"allow-list file unreadable: {path}") from exc
        if not isinstance(data, Mapping) or data.get("schema") != ALLOW_SCHEMA:
            raise AllowListError(f"{path}: not a {ALLOW_SCHEMA} file")
        entries = []
        for item in data.get("entries") or []:
            try:
                entry = AllowEntry(**item)
                net = entry.as_network()
            except (TypeError, ValueError) as exc:
                raise AllowListError(f"{path}: bad entry {item!r}") from exc
            if not (net.is_private or net.is_loopback or net.is_link_local):
                raise AllowListError(f"{path}: public range {entry.network} refused")
            entries.append(entry)
        return cls(entries)

    @classmethod
    def load_or_empty(cls, path: Path) -> "AllowList":
        return cls.load(path) if Path(path).exists() else cls()

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        body = {"schema": ALLOW_SCHEMA, "entries": [asdict(e) for e in self.entries]}
        tmp.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", "utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)

    def networks(self) -> tuple[Any, ...]:
        return tuple(e.as_network() for e in self.entries)

    def add(self, entries: Iterable[AllowEntry]) -> list[AllowEntry]:
        """Add entries not already present. Returns the ones added."""
        have = {(e.as_network(), e.port) for e in self.entries}
        added = []
        for e in entries:
            key = (e.as_network(), e.port)
            if key not in have:
                have.add(key)
                self.entries.append(e)
                added.append(e)
        return added

    def remove(self, network: str) -> list[AllowEntry]:
        """Remove every entry for ``network``. Returns the ones removed."""
        target = ipaddress.ip_network(network, strict=False)
        gone = [e for e in self.entries if e.as_network() == target]
        self.entries = [e for e in self.entries if e.as_network() != target]
        return gone


def manual_entry(value: str, port: Optional[int] = None) -> AllowEntry:
    """An operator-added entry: an IP literal or private CIDR range."""
    try:
        net = ipaddress.ip_network(value.strip(), strict=False)
    except ValueError as exc:
        raise AllowListError(
            f"allow entries are IP addresses or CIDR ranges, not {value!r}"
        ) from exc
    if not (net.is_private or net.is_loopback or net.is_link_local):
        raise AllowListError(f"public range {value!r} refused: it would leave the site")
    return AllowEntry(network=str(net), origin="manual", port=port, added_at=_utc())


def derive(
    profile: Path, allow_file: Path, resolver: Optional[Resolver] = None
) -> tuple[list[AllowEntry], list[AllowEntry]]:
    """Add the session profile's host(s) to ``allow_file``.

    Returns ``(added, already_present)``. Nothing else is added: not the
    subnet, not other hosts on the LAN.
    """
    entries = entries_for_targets(targets_from_file(profile), resolver)
    allow = AllowList.load_or_empty(allow_file)
    added = allow.add(entries)
    present = [e for e in entries if e not in added]
    allow.save(allow_file)
    return added, present


def describe(entry: AllowEntry) -> str:
    """One line for the operator."""
    where = f" port {entry.port}" if entry.port else ""
    if entry.origin == "derived":
        name = (
            f"{entry.host} "
            if entry.host and entry.host != entry.network.split("/")[0]
            else ""
        )
        return f"{entry.network}{where}  ({name}{entry.role} host, from {entry.source})"
    return f"{entry.network}{where}  (added by hand)"
