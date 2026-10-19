"""Where the mobile decision portal is allowed to listen, and fail-closed rules.

The portal is loopback-only by default.  Serving a phone requires the customer
to terminate trusted TLS themselves -- an enterprise reverse proxy, VPN, or
ZTNA hostname -- and to say so explicitly in configuration.  Three rules make
that safe:

1.  ``loopback`` (the default) binds ``127.0.0.1`` and advertises a loopback
    pairing URL.  A phone cannot reach it, and that is the correct behaviour
    for an unconfigured deployment.
2.  ``customer_ingress`` still binds loopback unless a **specific** bind
    address is named.  The reverse proxy is expected to run beside the runner
    and forward to loopback, which is the shape the design recommends.
3.  Every widening step is explicit and fails closed.  A missing public origin,
    a plaintext origin, a wildcard bind address, or a missing operator
    acknowledgement raises :class:`IngressError` and the portal does not start.

There is deliberately no "just bind everything for testing" switch.  Tests
construct :class:`PortalIngress` through the same validated resolver using a
loopback bind, which is what production uses too.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

#: The only address a portal binds unless a customer names a specific one.
LOOPBACK_HOST = "127.0.0.1"

#: Bind addresses that would expose the portal on every interface.  Never
#: permitted, in any mode, for any reason.
WILDCARD_BIND_HOSTS = frozenset({"", "*", "0.0.0.0", "::", "[::]", "0", "::0"})

INGRESS_MODES = frozenset({"loopback", "customer_ingress"})


class IngressError(RuntimeError):
    """A fail-closed ingress misconfiguration.  The portal must not start."""


@dataclass(frozen=True)
class PortalIngress:
    """A validated answer to "where does the portal listen and advertise?".

    Attributes:
        mode: ``loopback`` or ``customer_ingress``.
        bind_host: The exact address passed to ``bind()``.
        port: Requested TCP port; ``0`` selects an ephemeral port.
        public_origin: Scheme + authority a paired phone should use.  For
            ``loopback`` this is a ``http://127.0.0.1:<port>`` origin that only
            works on this computer.
        reachable_from_phone: Whether this configuration can serve a phone at
            all.  ``False`` for loopback, and the pairing surface says so
            instead of minting a link that cannot work.
    """

    mode: str
    bind_host: str
    port: int
    public_origin: str
    reachable_from_phone: bool

    @property
    def loopback_only(self) -> bool:
        """Whether the socket is bound to loopback (independent of ingress)."""
        return self.bind_host == LOOPBACK_HOST

    def pairing_url(self, secret: str) -> str:
        """Build the HTTPS link a QR code carries for one pairing secret.

        The secret is placed in the URL **fragment**.  Browsers never send a
        fragment in an HTTP request, so the one-use secret cannot land in a
        reverse-proxy access log, a referrer header, or a server log on the way
        to the phone's JavaScript.
        """
        return f"{self.public_origin}/pair#c={secret}"

    def describe(self) -> dict[str, Any]:
        """A settings-surface summary that never contains a secret."""
        return {
            "mode": self.mode,
            "bind_host": self.bind_host,
            "loopback_only": self.loopback_only,
            "public_origin": self.public_origin,
            "reachable_from_phone": self.reachable_from_phone,
        }


def _validated_public_origin(raw: str) -> str:
    """Accept exactly one https scheme + authority, or fail closed."""
    value = str(raw or "").strip()
    if not value or len(value) > 255:
        raise IngressError(
            "Customer ingress requires openadapt_portal_public_origin, the exact "
            "https origin your reverse proxy or VPN publishes for this runner."
        )
    parsed = urlsplit(value)
    if parsed.scheme != "https":
        raise IngressError(
            "The portal ingress origin must be https. OpenAdapt does not serve "
            "protected decision evidence over plaintext or a self-signed bypass."
        )
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise IngressError(
            "The portal ingress origin must be a bare https origin such as "
            "https://openadapt.clinic.example, with no path, query, or credentials."
        )
    if parsed.hostname.endswith("."):
        raise IngressError("The portal ingress origin has an invalid hostname")
    try:
        port = parsed.port
    except ValueError as exc:
        raise IngressError("The portal ingress origin has an invalid port") from exc
    host = parsed.hostname.lower()
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != 443:
        authority = f"{authority}:{port}"
    return f"https://{authority}"


def _validated_bind_host(raw: str) -> str:
    """Accept one specific non-wildcard address, or fail closed."""
    value = str(raw or "").strip().lower()
    if value in WILDCARD_BIND_HOSTS:
        raise IngressError(
            "The portal refuses a wildcard bind address. Name the exact interface "
            "address your ingress forwards to, or leave it unset so the portal "
            "stays on 127.0.0.1 behind a same-host reverse proxy."
        )
    try:
        address = ipaddress.ip_address(value.strip("[]"))
    except ValueError as exc:
        raise IngressError(
            "openadapt_portal_bind_host must be a literal IP address on this "
            "machine, not a hostname."
        ) from exc
    if address.is_unspecified or address.is_multicast:
        raise IngressError("The portal refuses an unspecified or multicast bind address")
    return str(address)


def resolve_ingress(config: Any, *, port: int | None = None) -> PortalIngress:
    """Resolve and validate the portal's listening and advertised addresses.

    Args:
        config: An :class:`~engine.config.EngineConfig` (any object exposing the
            ``portal_*`` attributes).
        port: Overrides ``config.portal_port``; ``0`` requests an ephemeral port.

    Returns:
        The validated :class:`PortalIngress`.

    Raises:
        IngressError: For any configuration that is not explicitly and
            completely authorized.  The portal must not start on this error.
    """
    mode = str(getattr(config, "portal_ingress_mode", "loopback") or "loopback").strip()
    if mode not in INGRESS_MODES:
        raise IngressError(
            f"Unknown portal ingress mode {mode!r}; expected one of "
            f"{', '.join(sorted(INGRESS_MODES))}."
        )
    requested_port = int(getattr(config, "portal_port", 0) if port is None else port)
    if not 0 <= requested_port <= 65535:
        raise IngressError("The portal port must be between 0 and 65535")

    if mode == "loopback":
        # A loopback portal is complete and useful on this computer. It simply
        # cannot serve a phone, and the pairing surface must say so rather than
        # advertise a link that silently fails on the customer's network.
        for field in ("portal_public_origin", "portal_bind_host"):
            if str(getattr(config, field, "") or "").strip():
                raise IngressError(
                    f"{field} is set but openadapt_portal_ingress_mode is "
                    "'loopback'. Set the mode to 'customer_ingress' to publish "
                    "the portal through your own HTTPS/VPN ingress."
                )
        return PortalIngress(
            mode="loopback",
            bind_host=LOOPBACK_HOST,
            port=requested_port,
            public_origin=f"http://{LOOPBACK_HOST}:{requested_port}",
            reachable_from_phone=False,
        )

    if not bool(getattr(config, "portal_ingress_acknowledged", False)):
        raise IngressError(
            "Publishing the portal beyond loopback requires "
            "openadapt_portal_ingress_acknowledged=true. Setting it records that "
            "your organization operates the trusted HTTPS/VPN ingress in front "
            "of this runner and has reviewed protected evidence on phones."
        )
    public_origin = _validated_public_origin(getattr(config, "portal_public_origin", ""))
    raw_bind = str(getattr(config, "portal_bind_host", "") or "").strip()
    bind_host = _validated_bind_host(raw_bind) if raw_bind else LOOPBACK_HOST
    return PortalIngress(
        mode="customer_ingress",
        bind_host=bind_host,
        port=requested_port,
        public_origin=public_origin,
        reachable_from_phone=True,
    )
