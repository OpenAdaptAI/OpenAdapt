"""Origin-bound capability broker and taint firewall for ``openadapt wrap``.

Prevents prompt-injected cross-origin exfiltration and SSRF during wrapped
browser runs without breaking legitimate enterprise SSO redirects or
explicitly configured on-premises intranet applications:

- **Provenance-Split URL Lattice:** Distinguishes ``PACK_TRUSTED`` origins
  explicitly declared at Step 0 from ``UNTRUSTED_DERIVED`` URLs (observed in
  untrusted DOM/text/tool output or outside the Step-0 allowlist). RFC1918
  intranet hosts (for example, on-prem ``10.x.x.x`` apps) are allowed ONLY
  when explicitly listed in ``allowed_origins`` with ``PACK_TRUSTED``
  provenance, while RFC1918/loopback/link-local/reserved SSRF targets are
  strictly blocked for any ``UNTRUSTED_DERIVED`` URL or unlisted host.
- **GET Query-String Exfiltration Guard:** Blocks outbound ``GET`` URLs whose
  query string carries registered sensitive entity values to a different
  origin than the origin where that value was observed.
- **Server-Issued SSO Redirects:** Explicitly allows server-issued HTTP
  301/302/303/307/308 redirects between ``allowed_origins`` and configured
  ``tenant_sso_origins`` while still blocking SSRF targets, query-string
  exfiltration, and third-party redirect destinations.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote_plus, urlsplit

DECISION_ALLOW = "allow"
DECISION_BLOCK = "block"

PROVENANCE_PACK_TRUSTED = "pack_trusted"
PROVENANCE_UNTRUSTED_DERIVED = "untrusted_derived"

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_SERVER_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"'()\[\]{}]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?"
_MIN_SENSITIVE_LEN = 4

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
class SensitiveValueRecord:
    """A sensitive entity token bound to the origin where it was observed."""

    value: str
    normalized: str
    compact_digits: str
    source_origin: Origin
    field_name: str = ""


@dataclass(frozen=True)
class FirewallDecision:
    """Deterministic allow/block decision for a navigation or fetch target."""

    url: str
    decision: str  # DECISION_ALLOW | DECISION_BLOCK
    code: str
    detail: str
    origin: Origin | None = None
    tainted: bool = False
    provenance: str = PROVENANCE_UNTRUSTED_DERIVED


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


def _digits_only(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


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
        self._sensitive_values: list[SensitiveValueRecord] = []

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
        """Step-0 ``PACK_TRUSTED`` task origins."""
        return self._allowed

    @property
    def tenant_sso_origins(self) -> frozenset[Origin]:
        """Configured tenant SSO identity-provider origins."""
        return self._sso

    def register_sensitive_value(
        self,
        value: str,
        *,
        source_origin: str | Origin,
        field_name: str = "",
    ) -> bool:
        """Register a sensitive entity token bound to ``source_origin``.

        Any subsequent outbound ``GET`` carrying this token in its query string
        to a different origin is blocked as query-string exfiltration.
        """
        origin = (
            source_origin
            if isinstance(source_origin, Origin)
            else parse_origin(source_origin)
        )
        if origin is None or not isinstance(value, str):
            return False
        cleaned = value.strip()
        if len(cleaned) < _MIN_SENSITIVE_LEN:
            return False
        rec = SensitiveValueRecord(
            value=cleaned,
            normalized=cleaned.lower(),
            compact_digits=_digits_only(cleaned),
            source_origin=origin,
            field_name=field_name,
        )
        if rec not in self._sensitive_values:
            self._sensitive_values.append(rec)
        return True

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

    def _check_ssrf(self, origin: Origin, *, provenance: str) -> tuple[bool, str]:
        """Enforce Provenance-Split URL Lattice for restricted/RFC1918 targets.

        Only ``PACK_TRUSTED`` origins explicitly listed in ``allowed_origins``
        may reach RFC1918 intranet or loopback hosts. Any ``UNTRUSTED_DERIVED``
        URL or unlisted origin resolving to a private, loopback, link-local,
        or reserved IP is blocked.
        """
        if provenance == PROVENANCE_PACK_TRUSTED and origin in self._allowed:
            return False, ""

        host = origin.hostname
        if host == "localhost" or host.endswith(".localhost"):
            return True, f"Blocked loopback hostname {host!r}."

        try:
            ipaddress.ip_address(host)
            is_literal_ip = True
        except ValueError:
            is_literal_ip = False

        if is_literal_ip:
            if _is_restricted_ip(host):
                return True, f"Blocked restricted IP target {host!r}."
            return False, ""

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

    def _check_query_exfiltration(
        self, url: str, dest_origin: Origin, *, method: str
    ) -> tuple[bool, str]:
        """Block outbound GET query strings carrying sensitive values cross-origin."""
        if method.upper() != "GET" or not self._sensitive_values:
            return False, ""
        raw_query = urlsplit(url).query
        if not raw_query:
            return False, ""

        decoded_parts: list[str] = [raw_query.lower(), unquote_plus(raw_query).lower()]
        for key, val in parse_qsl(raw_query, keep_blank_values=True):
            decoded_parts.append(key.lower())
            decoded_parts.append(val.lower())
        haystack = "\n".join(decoded_parts)
        haystack_digits = _digits_only(haystack)

        for rec in self._sensitive_values:
            if dest_origin == rec.source_origin:
                continue
            token_found = rec.normalized in haystack
            if not token_found and len(rec.compact_digits) >= 6:
                token_found = rec.compact_digits in haystack_digits
            if token_found:
                label = rec.field_name or "sensitive_value"
                return (
                    True,
                    f"Blocked GET query-string exfiltration of {label!r} from "
                    f"{rec.source_origin.as_str()} to {dest_origin.as_str()}.",
                )
        return False, ""

    def check_navigation(
        self,
        url: str,
        *,
        method: str = "GET",
        provenance: str | None = None,
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
        if provenance is None:
            if origin in self._allowed and not tainted:
                effective_provenance = PROVENANCE_PACK_TRUSTED
            else:
                effective_provenance = PROVENANCE_UNTRUSTED_DERIVED
        else:
            effective_provenance = provenance

        # 1. Cross-origin GET query-string exfiltration check
        exfil_blocked, exfil_detail = self._check_query_exfiltration(
            raw, origin, method=method
        )
        if exfil_blocked:
            return FirewallDecision(
                url=raw,
                decision=DECISION_BLOCK,
                code="query_string_exfiltration",
                detail=exfil_detail,
                origin=origin,
                tainted=tainted,
                provenance=effective_provenance,
            )

        # 2. Provenance-Split SSRF / RFC1918 check
        ssrf_blocked, ssrf_detail = self._check_ssrf(
            origin, provenance=effective_provenance
        )
        if ssrf_blocked:
            return FirewallDecision(
                url=raw,
                decision=DECISION_BLOCK,
                code="ssrf_restricted_target",
                detail=ssrf_detail,
                origin=origin,
                tainted=tainted,
                provenance=effective_provenance,
            )

        # 3. Server-issued HTTP 3xx SSO redirect (e.g. app -> IdP -> app callback)
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
                    provenance=effective_provenance,
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
                provenance=effective_provenance,
            )

        # 4. Tainted cross-origin URL outside Step-0 allowlist
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
                provenance=effective_provenance,
            )

        # 5. Step-0 allowed origin check
        if origin in self._allowed:
            return FirewallDecision(
                url=raw,
                decision=DECISION_ALLOW,
                code="allowed_origin",
                detail=f"Allowed Step-0 origin {origin.as_str()}.",
                origin=origin,
                tainted=tainted,
                provenance=effective_provenance,
            )

        return FirewallDecision(
            url=raw,
            decision=DECISION_BLOCK,
            code="origin_not_allowed",
            detail=f"Blocked navigation to non-allowlisted origin {origin.as_str()}.",
            origin=origin,
            tainted=tainted,
            provenance=effective_provenance,
        )


__all__ = [
    "DECISION_ALLOW",
    "DECISION_BLOCK",
    "FirewallDecision",
    "IpResolver",
    "Origin",
    "OriginCapabilityFirewall",
    "PROVENANCE_PACK_TRUSTED",
    "PROVENANCE_UNTRUSTED_DERIVED",
    "SensitiveValueRecord",
    "TaintRecord",
    "extract_origins_from_urls",
    "parse_origin",
]
