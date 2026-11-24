"""Origin-bound capability broker and taint firewall for ``openadapt wrap``.

Prevents prompt-injected cross-origin exfiltration and SSRF during wrapped
browser runs without breaking legitimate enterprise SSO redirects:

- Extracts Step-0 allowed origins (scheme, host, port) from initial task URLs.
- Marks URLs observed in untrusted text, DOM content, or tool outputs as
  ``TAINTED``.
- Blocks non-HTTP(S) schemes (``javascript:``, ``data:``, ``file:``, etc.).
- Blocks RFC1918 private networks, loopback, link-local / cloud-metadata
  (``169.254.169.254``), multicast, and reserved IP targets via both literal
  IP inspection and DNS resolution (using a mockable resolver for offline
  tests), unless that exact origin was explicitly allowlisted at Step 0.
- Blocks tainted cross-origin URLs outside ``allowed_origins``.
- Explicitly allows server-issued HTTP 301/302/303/307/308 SSO redirects
  originating from an allowed or tenant SSO origin to a configured
  ``tenant_sso_origins`` or ``allowed_origins`` target (while still blocking
  SSRF targets and untrusted third-party redirect destinations).
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

DECISION_ALLOW = "allow"
DECISION_BLOCK = "block"

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_SERVER_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"'()\[\]{}]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?"

IpResolver = Callable[[str], Sequence[str]]


@dataclass(frozen=True)
class Origin:
    """Normalized ``(scheme, hostname, port)`` tuple."""

    scheme: str
    hostname: str
    port: int

    def as_str(self) -> str:
        default_port = 443 if self.scheme == "https" else 80
        host = f"[{self.hostname}]" if ":" in self.hostname else self.hostname
        if self.port == default_port:
            return f"{self.scheme}://{host}"
        return f"{self.scheme}://{host}:{self.port}"


@dataclass(frozen=True)
class TaintRecord:
    """Record of an untrusted URL observed in DOM, text, or tool output."""

    url: str
    origin: Origin
    source: str


@dataclass(frozen=True)
class FirewallDecision:
    """Deterministic allow/block decision for a navigation or fetch target."""

    url: str
    decision: str  # DECISION_ALLOW | DECISION_BLOCK
    code: str
    detail: str
    origin: Origin | None = None
    tainted: bool = False


def parse_origin(url: str) -> Origin | None:
    """Parse an HTTP(S) URL into a normalized :class:`Origin`, or None."""
    if not isinstance(url, str) or not url.strip():
        return None
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return None
    hostname = parts.hostname
    if not hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return Origin(scheme=scheme, hostname=hostname.lower().rstrip("."), port=port)


def extract_origins_from_urls(urls: Iterable[str]) -> tuple[Origin, ...]:
    """Extract unique normalized HTTP(S) origins from Step-0 task URLs."""
    seen: dict[Origin, None] = {}
    for raw in urls:
        origin = parse_origin(raw)
        if origin is not None:
            seen[origin] = None
    return tuple(seen.keys())


def _is_restricted_ip(ip_text: str) -> bool:
    """Return True if ``ip_text`` is private, loopback, link-local, or reserved."""
    try:
        addr = ipaddress.ip_address(ip_text)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _default_dns_resolver(hostname: str) -> tuple[str, ...]:
    """Resolve ``hostname`` to IP address strings via the OS resolver."""
    infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    ips: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip_str = str(sockaddr[0])
        if ip_str not in ips:
            ips.append(ip_str)
    return tuple(ips)


def _normalize_url_key(url: str) -> str:
    """Normalize a URL for taint lookup (scheme + netloc + path, stripped query)."""
    parts = urlsplit(url.strip())
    origin = parse_origin(url)
    if origin is None:
        return url.strip().lower()
    path = parts.path or "/"
    return f"{origin.as_str()}{path}"


class OriginCapabilityFirewall:
    """Origin-bound capability broker and taint firewall for wrapped runs."""

    def __init__(
        self,
        allowed_origins: Iterable[str | Origin],
        *,
        tenant_sso_origins: Iterable[str | Origin] = (),
        resolver: IpResolver | None = None,
    ) -> None:
        self._allowed: frozenset[Origin] = frozenset(
            self._coerce_origins(allowed_origins)
        )
        self._sso: frozenset[Origin] = frozenset(
            self._coerce_origins(tenant_sso_origins)
        )
        self._resolver: IpResolver = (
            resolver if resolver is not None else _default_dns_resolver
        )
        self._tainted_urls: dict[str, TaintRecord] = {}
        self._tainted_origins: dict[Origin, TaintRecord] = {}

    @staticmethod
    def _coerce_origins(items: Iterable[str | Origin]) -> list[Origin]:
        out: list[Origin] = []
        for item in items:
            if isinstance(item, Origin):
                out.append(item)
            else:
                parsed = parse_origin(item)
                if parsed is None:
                    raise ValueError(f"Invalid HTTP(S) origin: {item!r}")
                out.append(parsed)
        return out

    @property
    def allowed_origins(self) -> frozenset[Origin]:
        """Step-0 allowed task origins."""
        return self._allowed

    @property
    def tenant_sso_origins(self) -> frozenset[Origin]:
        """Configured tenant SSO identity-provider origins."""
        return self._sso

    def mark_tainted_url(self, url: str, *, source: str = "dom") -> bool:
        """Mark a URL observed in untrusted content as ``TAINTED``."""
        origin = parse_origin(url)
        if origin is None:
            return False
        rec = TaintRecord(url=url.strip(), origin=origin, source=source)
        self._tainted_urls[_normalize_url_key(url)] = rec
        self._tainted_origins.setdefault(origin, rec)
        return True

    def record_untrusted_text(self, text: str, *, source: str = "dom") -> int:
        """Scan untrusted text/DOM/tool output and taint all embedded HTTP(S) URLs."""
        if not text:
            return 0
        count = 0
        for match in _URL_IN_TEXT_RE.finditer(text):
            candidate = match.group(0).rstrip(_TRAILING_PUNCT)
            if self.mark_tainted_url(candidate, source=source):
                count += 1
        return count

    def is_tainted(self, url: str) -> bool:
        """Return True if ``url`` or its origin was observed in untrusted content."""
        origin = parse_origin(url)
        if origin is None:
            return False
        return (
            _normalize_url_key(url) in self._tainted_urls
            or origin in self._tainted_origins
        )

    def _check_ssrf(self, origin: Origin) -> tuple[bool, str]:
        """Return ``(blocked, detail)`` for literal or DNS-resolved restricted IPs."""
        # If the exact origin was explicitly allowlisted at Step 0 (for example,
        # a local test harness on 127.0.0.1:3000), permit that exact origin only.
        if origin in self._allowed:
            return False, ""

        host = origin.hostname
        if host == "localhost" or host.endswith(".localhost"):
            return True, f"Blocked loopback hostname {host!r}."

        # 1. Literal IP check
        try:
            ipaddress.ip_address(host)
            is_literal_ip = True
        except ValueError:
            is_literal_ip = False

        if is_literal_ip:
            if _is_restricted_ip(host):
                return True, f"Blocked restricted IP target {host!r}."
            return False, ""

        # 2. DNS resolution check (stops on uncertainty if resolution fails)
        try:
            resolved_ips = tuple(self._resolver(host))
        except (OSError, ValueError) as exc:
            return True, f"DNS resolution failed for {host!r}: {exc}"

        if not resolved_ips:
            return True, f"DNS resolution returned no addresses for {host!r}."

        for ip_str in resolved_ips:
            if _is_restricted_ip(ip_str):
                return (
                    True,
                    f"Blocked SSRF target: {host!r} resolved to restricted IP {ip_str!r}.",
                )
        return False, ""

    def check_navigation(
        self,
        url: str,
        *,
        is_server_redirect: bool = False,
        redirect_status: int | None = None,
        redirect_from_origin: str | Origin | None = None,
    ) -> FirewallDecision:
        """Decide whether navigating to or fetching ``url`` is allowed."""
        raw = url.strip() if isinstance(url, str) else ""
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            return FirewallDecision(
                url=raw,
                decision=DECISION_BLOCK,
                code="disallowed_scheme",
                detail=f"Blocked non-HTTP(S) scheme {scheme!r}.",
            )

        origin = parse_origin(raw)
        if origin is None:
            return FirewallDecision(
                url=raw,
                decision=DECISION_BLOCK,
                code="malformed_url",
                detail=f"Blocked malformed URL {raw!r}.",
            )

        tainted = self.is_tainted(raw)

        # 1. SSRF / private network check (always enforced for non-allowlisted origins)
        ssrf_blocked, ssrf_detail = self._check_ssrf(origin)
        if ssrf_blocked:
            return FirewallDecision(
                url=raw,
                decision=DECISION_BLOCK,
                code="ssrf_restricted_target",
                detail=ssrf_detail,
                origin=origin,
                tainted=tainted,
            )

        # 2. Server-issued HTTP 3xx SSO redirect (e.g. app -> IdP -> app callback)
        if is_server_redirect and redirect_status in _SERVER_REDIRECT_STATUSES:
            from_origin = (
                redirect_from_origin
                if isinstance(redirect_from_origin, Origin)
                else parse_origin(redirect_from_origin or "")
            )
            trusted_sources = self._allowed | self._sso
            trusted_targets = self._allowed | self._sso
            if from_origin in trusted_sources and origin in trusted_targets:
                return FirewallDecision(
                    url=raw,
                    decision=DECISION_ALLOW,
                    code="server_sso_redirect",
                    detail=(
                        f"Allowed server-issued HTTP {redirect_status} redirect "
                        f"from {from_origin.as_str()} to {origin.as_str()}."
                    ),
                    origin=origin,
                    tainted=tainted,
                )
            return FirewallDecision(
                url=raw,
                decision=DECISION_BLOCK,
                code="untrusted_redirect_target",
                detail=(
                    f"Blocked HTTP {redirect_status} redirect to non-allowlisted "
                    f"origin {origin.as_str()}."
                ),
                origin=origin,
                tainted=tainted,
            )

        # 3. Tainted cross-origin URL outside Step-0 allowlist
        if tainted and origin not in self._allowed:
            return FirewallDecision(
                url=raw,
                decision=DECISION_BLOCK,
                code="tainted_cross_origin_url",
                detail=(
                    f"Blocked tainted URL on non-allowlisted origin {origin.as_str()}."
                ),
                origin=origin,
                tainted=True,
            )

        # 4. Step-0 allowed origin check
        if origin in self._allowed:
            return FirewallDecision(
                url=raw,
                decision=DECISION_ALLOW,
                code="allowed_origin",
                detail=f"Allowed Step-0 origin {origin.as_str()}.",
                origin=origin,
                tainted=tainted,
            )

        return FirewallDecision(
            url=raw,
            decision=DECISION_BLOCK,
            code="origin_not_allowed",
            detail=f"Blocked navigation to non-allowlisted origin {origin.as_str()}.",
            origin=origin,
            tainted=tainted,
        )


__all__ = [
    "DECISION_ALLOW",
    "DECISION_BLOCK",
    "FirewallDecision",
    "IpResolver",
    "Origin",
    "OriginCapabilityFirewall",
    "TaintRecord",
    "extract_origins_from_urls",
    "parse_origin",
]
